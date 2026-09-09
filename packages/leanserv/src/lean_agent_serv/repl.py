"""One warm `leankernel serve` process (M1.8.1) and the crash taxonomy around it (spec §6.2's
worker model). `pool.py` (a later milestone) manages many of these, keyed by `base_env_digest`,
with LRU eviction; this module only knows how to run exactly one.

Deliberately minimal request/response shape: `CheckResult` here is `(ok, diagnostics)`, not the
full spec §6.2 `CheckRequest`/`CheckResult` (`base_env_digest`, `options`, `want`, ...). This
worker is already scoped to one base env by which imports it was spawned with, and
`LeanKernel.Serve`'s own wire protocol (M1.8.1) doesn't carry `options`/`want` yet either --
adding either side now, with no caller that needs `CheckOptions` or infotree/axiom extraction,
would be speculative surface area. Extend both together when a real caller needs more.
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, Self

#: Spec §6.2's default: 300 s per command, overridable per request.
DEFAULT_TIMEOUT_MS = 300_000


class ReplCrashed(Exception):
    """Base for any condition that leaves this worker unusable -- the caller (eventually
    `pool.py`) must discard it rather than send it another request. Never raised for an ordinary
    elaboration failure (a type error is a normal, expected `CheckResult` with `ok=False`, not a
    crash) -- spec's own principle that `infra_error` is a distinct, unbudgeted outcome from a
    proof failure applies here one layer down: a crashed worker is an infra concern, not evidence
    about whatever body it was last asked to check.
    """

    def __init__(self, message: str, *, returncode: int | None, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class ReplTimeout(ReplCrashed):
    """No response within the request's wallclock budget. The process has already been
    SIGKILLed by the time this is raised -- spec §6.2: "External SIGKILL" is the second of the
    two timeout mechanisms a Lean worker needs, since `maxHeartbeats` alone does not bound
    `native_decide`'s compiler invocation, deep `decide` recursion, or elaboration-time `IO`.
    """


class ReplExited(ReplCrashed):
    """The process is gone (or its pipes closed) for a reason other than our own timeout-
    triggered kill: it crashed, was OOM-killed, or otherwise exited on its own."""


class ReplProtocolError(ReplCrashed):
    """The process is alive and responded, but not with a well-formed, correlated response.
    `LeanKernel.Serve.handleLine` (M1.8.1) guarantees a response line for every input line, even
    a malformed one -- so a response that isn't valid JSON, or whose `id` doesn't match the
    request just sent, means stdout desynchronized from the request stream, not merely a Lean-
    side elaboration error. The worker's internal state can no longer be trusted either way, so
    this is treated as a crash rather than something a retry could paper over.
    """


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a `check` request that the worker genuinely answered -- `ok=False` here means
    a real elaboration failure (e.g. a type error), not a crash; see `ReplCrashed`."""

    ok: bool
    diagnostics: tuple[str, ...] = field(default_factory=tuple)


class ReplWorker:
    """Wraps one `lake exe leankernel serve [<import>...]` process (M1.8.1). Construct via
    `ReplWorker.spawn`, not the constructor directly -- spawning is async (starting the process
    and its stderr-drain task) and there is no meaningful half-constructed state to expose.
    """

    def __init__(self, process: asyncio.subprocess.Process, imports: tuple[str, ...]) -> None:
        self._process = process
        self.imports = imports
        self._next_id = 0
        self._stderr_lines: list[str] = []
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    @classmethod
    async def spawn(cls, lake_project_dir: Path, imports: tuple[str, ...] = ()) -> Self:
        """Start a fresh worker with a warm environment built from `imports` (spec's
        `base_env`.`recipe.imports`, in the minimal form M1.8.1 accepts: bare module names, no
        options/opens/prelude yet). `lake_project_dir` is `packages/leankernel` in this repo, but
        is not hardcoded here -- keeping deployment-path decisions with the caller, which is what
        will eventually also own the base-env-to-imports mapping (`pool.py`).
        """
        process = await asyncio.create_subprocess_exec(
            "lake",
            "exe",
            "leankernel",
            "serve",
            *imports,
            cwd=lake_project_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # `lake exe` does not exec-replace itself -- it forks the actual `leankernel` binary
            # as its own child and stays alive supervising it (confirmed empirically: `ps aux`
            # during a hung check showed two separate PIDs, `lake exe leankernel serve` and
            # `leankernel serve`, both alive). Signalling only `self._process` (the `lake` PID)
            # therefore does not touch the grandchild actually doing the elaboration -- it is
            # simply orphaned and keeps running, and keeps its inherited copy of the stdout pipe's
            # write end open, so a reader on our side never even sees EOF. `start_new_session`
            # (POSIX `setsid`) puts `lake` and everything it forks into one new process group, so
            # `_kill` below can take out the whole group at once via `os.killpg`.
            start_new_session=True,
        )
        return cls(process, imports)

    @property
    def is_alive(self) -> bool:
        return self._process.returncode is None

    def _kill(self) -> None:
        """Signal the whole process group `spawn` placed this worker in, not just the immediate
        `lake` child -- see `spawn`'s `start_new_session` note for why killing only `lake` leaves
        the actual `leankernel` process (and whatever CPU-bound loop it's stuck in) running.
        `self._process.pid` is the group leader's pid, which is also the group id.
        """
        if self._process.returncode is not None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone

    async def _drain_stderr(self) -> None:
        """Continuously consume stderr for the process's whole lifetime, defensively: a pipe
        nobody reads fills its OS buffer once the child writes enough to it, and the child then
        blocks on that write forever -- indistinguishable from a genuine hang in whatever it was
        elaborating unless something is always draining the other end.

        This can't be exercised end-to-end through `check`'s normal request body the way the rest
        of this module's crash taxonomy is tested: `#eval`'s own `IO.println`/`IO.eprintln` output
        is captured into the command's message log and returned over *stdout* as ordinary
        diagnostics, not written to the process's real stderr fd at all -- confirmed empirically
        (a body running `IO.eprintln` thousands of times left `/dev/null`-redirected stderr at
        zero bytes; the output showed up in the JSON response's `diagnostics` array instead). In
        production, real traffic on this pipe is expected to be rare (a Lean panic, a C-runtime
        message, GC diagnostics) -- draining it is defense-in-depth for exactly that rare case,
        not something this module can manufacture on demand to prove.
        """
        assert self._process.stderr is not None
        async for raw in self._process.stderr:
            self._stderr_lines.append(raw.decode(errors="replace").rstrip("\n"))

    async def _kill_and_raise(self, exc_type: type[ReplCrashed], message: str) -> NoReturn:
        self._kill()
        await self._process.wait()
        raise exc_type(
            message, returncode=self._process.returncode, stderr="\n".join(self._stderr_lines)
        )

    async def check(self, body: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> CheckResult:
        """Elaborate `body` against this worker's warm environment. Raises a `ReplCrashed`
        subclass (never returns a `CheckResult`) if the worker stops being usable in the
        process -- callers that want to keep working must spawn a replacement.
        """
        if not self.is_alive:
            raise ReplExited(
                "worker already exited before this request was sent",
                returncode=self._process.returncode,
                stderr="\n".join(self._stderr_lines),
            )
        request_id = str(self._next_id)
        self._next_id += 1
        stdin, stdout = self._process.stdin, self._process.stdout
        assert stdin is not None and stdout is not None

        request_line = json.dumps({"id": request_id, "body": body}) + "\n"
        try:
            stdin.write(request_line.encode())
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            await self._kill_and_raise(ReplExited, f"stdin closed while writing request: {exc}")

        try:
            async with asyncio.timeout(timeout_ms / 1000):
                raw = await stdout.readline()
        except TimeoutError:
            self._kill()
            await self._process.wait()
            raise ReplTimeout(
                f"no response within {timeout_ms} ms",
                returncode=self._process.returncode,
                stderr="\n".join(self._stderr_lines),
            ) from None

        if raw == b"":
            await self._kill_and_raise(
                ReplExited, "stdout closed (process exited) while awaiting response"
            )

        try:
            response: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            await self._kill_and_raise(
                ReplProtocolError, f"response line was not valid JSON ({exc}): {raw!r}"
            )

        if response.get("id") != request_id:
            await self._kill_and_raise(
                ReplProtocolError,
                f"response id {response.get('id')!r} does not match request id "
                f"{request_id!r} -- worker desynchronized",
            )

        return CheckResult(
            ok=bool(response.get("ok")), diagnostics=tuple(response.get("diagnostics", []))
        )

    async def close(self) -> None:
        """Ask the process to exit cleanly (closing its stdin makes `runServe`'s loop see EOF
        and exit 0 -- see M1.8.1's `Serve.lean`), falling back to SIGKILL if it doesn't within a
        few seconds. Always safe to call, including on an already-exited process.
        """
        if self.is_alive:
            if self._process.stdin is not None:
                self._process.stdin.close()
            try:
                async with asyncio.timeout(5.0):
                    await self._process.wait()
            except TimeoutError:
                self._kill()
                await self._process.wait()
        await self._stderr_task

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
