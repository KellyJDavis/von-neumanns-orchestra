"""One warm `leankernel serve` process (M1.8.1) and the crash taxonomy around it (spec §6.2's
worker model). `pool.py` (a later milestone) manages many of these, keyed by `base_env_digest`,
with LRU eviction; this module only knows how to run exactly one.

Deliberately minimal request/response shape: `CheckResult` here is `(ok, diagnostics)`, not the
full spec §6.2 `CheckRequest`/`CheckResult` (`base_env_digest`, `options`, `want`, ...). This
worker is already scoped to one base env by which imports it was spawned with, and
`LeanKernel.Serve`'s own wire protocol doesn't carry `options`/`want` yet either -- adding either
side now, with no caller that needs `CheckOptions` or infotree/axiom extraction, would be
speculative surface area. Extend both together when a real caller needs more.

Four request kinds: `check` (M1.8.1), `seal` (M2.1.1), `link` (M2.1.2) and `decompose` (M2.1.3).
They share one pipe, one `_request` transport, and one crash taxonomy.
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import json
import os
import signal
from collections.abc import Sequence
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


@dataclass(frozen=True)
class SealGoal:
    """One goal to seal (spec §4.1). `name` is the unqualified declaration name -- `Serve.lean`
    puts it under `LeanAgent.Goals` and rejects anything that isn't a plain identifier.

    `level_params` are declared on the generated `def` (spec §4.1's `def G_<id>.{u_0}`). Required
    whenever `statement` mentions a universe by name, which a decomposed subgoal's printed
    statement routinely does: sealing forces `autoImplicit false`, so a free universe name is an
    error rather than something Lean binds. Empty for the ordinary monomorphic case.
    """

    name: str
    statement: str
    level_params: tuple[str, ...] = ()


@dataclass(frozen=True)
class SealedGoal:
    """Per-goal seal outcome. `ok=False` is a `seal_failed` for *this goal only* (spec §4.1: "If
    the statement does not elaborate, no obligation is created ... which is distinct from any
    proof outcome") -- its siblings in the same request are unaffected.
    """

    decl: str
    level_params: tuple[str, ...]
    diagnostics: tuple[str, ...]
    ok: bool


@dataclass(frozen=True)
class SealResult:
    """`goals` is parallel to the request's own goals, so a caller can create obligations for the
    entries that sealed and report the rest. `bundle_source` carries only the goals that *did*
    seal -- it is spec §4.1's generated bundle file, compiled lazily out of band, and nothing
    re-verifies it at that point.
    """

    ok: bool
    goals: tuple[SealedGoal, ...]
    bundle_source: str


@dataclass(frozen=True)
class DecomposedLemma:
    """One extracted subgoal (spec §4.6), in the form its consumer needs: `statement` is source
    text, because the next thing that happens to a child is a `seal` request, which takes text.

    `round_trips` is false when the printed statement does not seal back to the `Expr` it was
    printed from -- i.e. the text is not a faithful stand-in for the abstracted goal. A caller must
    not create an obligation from such a statement: it would prove something other than what the
    parent's reassembly needs, and the mismatch would only surface much later as a failing group.
    """

    name: str
    statement: str
    level_params: tuple[str, ...]
    round_trips: bool
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class DecomposeResult:
    """`ok` with an empty `lemmas` means the development genuinely contained no `sorry`; `ok=False`
    means it did not elaborate. An empty list alone cannot tell those apart and they call for
    opposite responses.

    `reassembly` is the original source with each `sorry` replaced by an application of its child
    lemma. Spec §4.6 is explicit that running it is a *full acceptance check* against the parent's
    sealed goal -- link, replay and audit -- not a text-substitution exercise; this is only the
    text.
    """

    ok: bool
    lemmas: tuple[DecomposedLemma, ...]
    reassembly: str
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class LinkResult:
    """One submission's whole acceptance path (spec §4.2-§4.4). The three `_ok` flags are separate
    because they check genuinely different things and `verdict` records all three -- see CLAUDE.md
    on why neither link nor replay subsumes the other.

    `goal_module`/`goal_olean_path` say where the sealed goal was actually imported from, so the
    caller can hash that artifact for `verdict.sealed_olean_sha_observed` rather than echoing back
    a digest it was handed. `None` for both means the goal wasn't an imported constant at all,
    which is itself a link failure.
    """

    ok: bool
    link_ok: bool
    replay_ok: bool
    axiom_audit_ok: bool
    axioms: tuple[str, ...]
    uses_sorry: bool
    uses_compiler_trust: bool
    replay_checked_count: int
    goal_module: str | None
    goal_olean_path: str | None
    diagnostics: tuple[str, ...]


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
    async def spawn(
        cls,
        lake_project_dir: Path,
        imports: tuple[str, ...] = (),
        *,
        extra_lean_path: Path | None = None,
    ) -> Self:
        """Start a fresh worker with a warm environment built from `imports` (spec's
        `base_env`.`recipe.imports`, in the minimal form M1.8.1 accepts: bare module names, no
        options/opens/prelude yet). `lake_project_dir` is `packages/leankernel` in this repo, but
        is not hardcoded here -- keeping deployment-path decisions with the caller, which is what
        will eventually also own the base-env-to-imports mapping (`pool.py`).

        `extra_lean_path` is prepended to `LEAN_PATH` so the worker can import modules that live
        outside the Lake package -- specifically, materialized sealed goal bundles (spec §4.1's
        `LeanAgent/Goals/Bundle_<digest>.lean`), which Link requires to be genuinely *imported*
        constants and which no Lake target could know about ahead of time. Confirmed empirically
        that `lake exe` merges an inherited `LEAN_PATH` with the one it computes for the workspace
        rather than replacing it: a worker started this way resolved both `Init` (from the
        toolchain, via Lake's own path) and a bundle module from an arbitrary directory.
        """
        env: dict[str, str] | None = None
        if extra_lean_path is not None:
            inherited = os.environ.get("LEAN_PATH", "")
            merged = (
                f"{extra_lean_path}{os.pathsep}{inherited}" if inherited else str(extra_lean_path)
            )
            env = {**os.environ, "LEAN_PATH": merged}

        process = await asyncio.create_subprocess_exec(
            "lake",
            "exe",
            "leankernel",
            "serve",
            *imports,
            cwd=lake_project_dir,
            env=env,
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

    @property
    def pid(self) -> int:
        """The `lake` process's own pid (also its process group id -- see `spawn`'s
        `start_new_session` note). Exposed for callers that need to inspect the OS process
        directly (e.g. `memory_probe.py`'s RSS measurement, spec gate 9) rather than reaching
        into `_process` from outside the class.
        """
        return self._process.pid

    def _kill(self) -> None:
        """Signal the whole process group `spawn` placed this worker in, not just the immediate
        `lake` child -- see `spawn`'s `start_new_session` note for why killing only `lake` leaves
        the actual `leankernel` process (and whatever CPU-bound loop it's stuck in) running.
        `self._process.pid` is the group leader's pid, which is also the group id.

        Deliberately does not early-return just because `self._process.returncode` is already
        set. A POSIX process group stays alive as long as *any* member process does, independent
        of whether the original leader (`lake`, `self._process`) has already exited -- `lake`
        exiting first and leaving its `leankernel` grandchild still finishing up is exactly the
        case `close()`'s graceful path needs this to still catch (confirmed empirically: without
        this, `close()` could return while `leankernel` was still alive for a few more seconds,
        visible in `ps aux` immediately afterward). `ProcessLookupError` -- meaning the whole
        group is already gone -- is the only condition that makes calling this a genuine no-op.
        """
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

    async def _request(self, payload: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        """Send one request line and return the one response line's decoded object, correlated by
        `id`. Every request kind shares this: `Serve.lean`'s protocol is one line in, exactly one
        line out, in order, whatever the `kind` -- so the transport, the crash taxonomy, and the
        id correlation are the same for all of them and only the payload shape differs.

        `id` is assigned here rather than by the caller, so a caller can never accidentally reuse
        one and turn a genuine desynchronization into a silently-accepted mismatched response.
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

        request_line = json.dumps({**payload, "id": request_id}) + "\n"
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

        return response

    async def check(self, body: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> CheckResult:
        """Elaborate `body` against this worker's warm environment. Raises a `ReplCrashed`
        subclass (never returns a `CheckResult`) if the worker stops being usable in the
        process -- callers that want to keep working must spawn a replacement.

        Sends no `kind` field. `Serve.lean` defaults a missing `kind` to `"check"`, and keeping
        this request byte-identical to the one M1.8.1 shipped is what keeps that default a tested
        path rather than an untested compatibility claim.
        """
        response = await self._request({"body": body}, timeout_ms)
        return CheckResult(
            ok=bool(response.get("ok")), diagnostics=tuple(response.get("diagnostics", []))
        )

    async def seal(
        self, goals: Sequence[SealGoal], *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> SealResult:
        """Seal `goals` as one bundle against this worker's warm environment (spec §4.1). Raises
        a `ReplCrashed` subclass on the same conditions `check` does.

        Nothing here waits on the build system: sealing is elaboration only, in the already-warm
        worker, and the `.olean` for `bundle_source` is produced lazily out of band ("the hot path
        never waits on the build system"). That is why this is a plain request on the same pipe as
        `check` rather than anything that needs a Lake invocation of its own.
        """
        response = await self._request(
            {
                "kind": "seal",
                "goals": [
                    {
                        "name": g.name,
                        "statement": g.statement,
                        "levelParams": list(g.level_params),
                    }
                    for g in goals
                ],
            },
            timeout_ms,
        )
        return SealResult(
            ok=bool(response.get("ok")),
            goals=tuple(
                SealedGoal(
                    decl=str(report.get("decl", "")),
                    level_params=tuple(report.get("levelParams", [])),
                    diagnostics=tuple(report.get("diagnostics", [])),
                    ok=bool(report.get("ok")),
                )
                for report in response.get("reports", [])
            ),
            bundle_source=str(response.get("bundleSource", "")),
        )

    async def decompose(
        self, development: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> DecomposeResult:
        """Extract every `sorry` from `development` as a standalone closed statement (spec §4.6),
        against this worker's warm environment. Raises a `ReplCrashed` subclass on the same
        conditions `check` does.

        Warm, like every other request kind here: spec §4.1's measurement (cold per-child
        compilation ~78% of pipeline time against under 1% warm) is what makes decomposition
        affordable at all, since a decomposition group multiplies the number of children.
        """
        response = await self._request({"kind": "decompose", "body": development}, timeout_ms)
        return DecomposeResult(
            ok=bool(response.get("ok")),
            lemmas=tuple(
                DecomposedLemma(
                    name=str(lemma.get("name", "")),
                    statement=str(lemma.get("statement", "")),
                    level_params=tuple(lemma.get("levelParams", [])),
                    round_trips=bool(lemma.get("roundTrips")),
                    diagnostics=tuple(lemma.get("diagnostics", [])),
                )
                for lemma in response.get("lemmas", [])
            ),
            reassembly=str(response.get("reassembly", "")),
            diagnostics=tuple(response.get("diagnostics", [])),
        )

    async def link(
        self,
        *,
        goal: str,
        entry: str,
        development: str,
        allow_axioms: Sequence[str],
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> LinkResult:
        """Run the whole acceptance path on one submission (spec §4.2-§4.4): elaborate
        `development` against this worker's warm environment, link `entry` against the sealed
        `goal`, replay, and audit. Raises a `ReplCrashed` subclass on the same conditions `check`
        does.

        This worker must have been spawned with the goal's bundle among its `imports` -- Link
        requires the goal to be an imported constant, and a goal that only ever existed in a warm
        session (the way `seal` leaves it) can never satisfy that. A worker without it comes back
        with `link_ok=False` and a diagnostic saying so, rather than silently linking against
        something else.

        `allow_axioms` is `run.axiom_allowlist` (plus `sorryAx` when `run.allow_sorry`). It is
        required, not defaulted: spec §4.4's audit is allowlist-driven, so an omitted allowlist
        must mean "permit nothing" rather than quietly substituting a default that happens to be
        permissive.
        """
        response = await self._request(
            {
                "kind": "link",
                "goal": goal,
                "entry": entry,
                "body": development,
                "allowAxioms": list(allow_axioms),
            },
            timeout_ms,
        )
        return LinkResult(
            ok=bool(response.get("ok")),
            link_ok=bool(response.get("linkOk")),
            replay_ok=bool(response.get("replayOk")),
            axiom_audit_ok=bool(response.get("axiomAuditOk")),
            axioms=tuple(response.get("axioms", [])),
            uses_sorry=bool(response.get("usesSorry")),
            uses_compiler_trust=bool(response.get("usesCompilerTrust")),
            replay_checked_count=int(response.get("replayCheckedCount", 0)),
            goal_module=response.get("goalModule"),
            goal_olean_path=response.get("goalOleanPath"),
            diagnostics=tuple(response.get("diagnostics", [])),
        )

    async def close(self) -> None:
        """Ask the process to exit cleanly (closing its stdin makes `runServe`'s loop see EOF
        and exit 0 -- see M1.8.1's `Serve.lean`), falling back to SIGKILL if it doesn't within a
        few seconds. Always safe to call, including on an already-exited process.

        Sweeps the whole process group with `_kill()` even after a clean exit, not only on the
        timeout path -- `self._process.wait()` returning only confirms `lake` (the direct child)
        is gone, not that its `leankernel` grandchild has finished exiting too (`lake` can exit
        first and leave it to wind down a moment later). Confirmed empirically: without this
        sweep, a real `leankernel` process was still visible in `ps aux` for several seconds after
        `close()` had already returned.
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
        self._kill()
        await self._stderr_task

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
