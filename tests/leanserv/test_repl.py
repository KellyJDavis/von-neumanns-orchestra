"""M1.8.2 exit criterion: `ReplWorker` (spawns `leankernel serve`, M1.8.1) round-trips real
checks and correctly classifies every way the underlying process can stop being usable --
timeout, exit, and protocol desync -- against a genuinely spawned process, never a mock (see
CLAUDE.md).

Local dev: `lake build` in `packages/leankernel` first (M1.8.1's `serve` subcommand must exist),
then `uv run pytest tests/leanserv`. CI builds `packages/leankernel` before this suite runs (see
`.github/workflows/ci.yml`) specifically so the skip below never triggers there.

No pytest-asyncio dependency, matching `tests/test_blobs.py`'s M1.7 precedent: plain sync
`def test_...` functions drive the async `ReplWorker` API via `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import pytest
from lean_agent_serv.repl import (
    DEFAULT_TIMEOUT_MS,
    ReplExited,
    ReplProtocolError,
    ReplTimeout,
    ReplWorker,
)

LEANKERNEL_DIR = Path(__file__).resolve().parents[2] / "packages" / "leankernel"
LEANKERNEL_EXE = LEANKERNEL_DIR / ".lake" / "build" / "bin" / "leankernel"


@pytest.fixture(scope="session")
def lake_project_dir() -> Path:
    # A missing built exe (Lean toolchain not set up, or `lake build` not yet run) skips
    # gracefully rather than failing every test with the same opaque "file not found" -- the same
    # honesty-over-fake-pass convention `tests/db/`'s Postgres-connectivity check uses.
    if not LEANKERNEL_EXE.exists():
        pytest.skip(
            f"{LEANKERNEL_EXE} not built; run `lake build` in {LEANKERNEL_DIR} first. "
            "CI always builds it before this suite runs (see .github/workflows/ci.yml)."
        )
    return LEANKERNEL_DIR


def test_check_ok_and_type_error(lake_project_dir: Path) -> None:
    async def run() -> None:
        async with await ReplWorker.spawn(lake_project_dir, ("Init",)) as worker:
            ok = await worker.check("def foo : Nat := 5")
            assert ok.ok
            assert ok.diagnostics == ()

            bad = await worker.check("def bad : Nat := true")
            assert not bad.ok
            assert bad.diagnostics
            assert worker.is_alive

    asyncio.run(run())


def test_isolation_between_requests(lake_project_dir: Path) -> None:
    """Two checks against the same warm worker must not see each other's declarations -- this
    re-confirms, across a real process boundary through `ReplWorker`'s own plumbing, what M1.8.1
    already proved for `Serve.lean`'s in-process dispatch logic directly.
    """

    async def run() -> None:
        async with await ReplWorker.spawn(lake_project_dir, ("Init",)) as worker:
            first = await worker.check("def onlyInFirst : Nat := 1")
            assert first.ok
            second = await worker.check("def usesFirst : Nat := onlyInFirst")
            assert not second.ok

    asyncio.run(run())


def test_timeout_kills_the_process(lake_project_dir: Path) -> None:
    """An infinite `IO` loop run via `#eval` hangs during elaboration itself, not merely during a
    bounded computation -- exactly the case spec §6.2 says `maxHeartbeats` cannot catch
    ("elaboration-time IO"), which is why the wallclock timeout with an external SIGKILL is a
    second, independent mechanism rather than redundant with it.
    """

    async def run() -> None:
        async with await ReplWorker.spawn(lake_project_dir, ("Init",)) as worker:
            with pytest.raises(ReplTimeout) as exc_info:
                await worker.check(
                    "partial def spin : IO Unit := spin\n#eval spin", timeout_ms=1000
                )
            assert not worker.is_alive
            assert exc_info.value.returncode is not None

    asyncio.run(run())


def test_process_killed_externally_is_reported_as_exited(lake_project_dir: Path) -> None:
    """Simulates an out-of-band crash (OOM kill, segfault) by sending the real process group a
    real SIGKILL directly, bypassing `ReplWorker` entirely, then confirming the next `check()`
    call reports it as `ReplExited` rather than hanging or raising something misleading.

    The whole *group*, via `os.killpg` -- not just `worker._process.pid` -- for the same reason
    `ReplWorker._kill` does: `lake exe` forks the actual `leankernel` binary as its own child
    rather than exec-replacing itself, so signalling only the `lake` pid leaves that grandchild
    running, orphaned, still holding the stdout pipe open. `spawn`'s `start_new_session=True` is
    what makes a single `killpg` reach both.
    """

    async def run() -> None:
        worker = await ReplWorker.spawn(lake_project_dir, ("Init",))
        try:
            assert (await worker.check("def foo : Nat := 1")).ok
            os.killpg(worker._process.pid, signal.SIGKILL)
            await worker._process.wait()

            with pytest.raises(ReplExited):
                await worker.check("def bar : Nat := 2")
        finally:
            await worker.close()

    asyncio.run(run())


def test_desynchronized_response_is_a_protocol_error(lake_project_dir: Path) -> None:
    """Writes an extra, well-formed request directly to the process's stdin ahead of a normal
    `check()` call. `Serve.lean` answers every line in order, so `check()`'s own `readline()`
    reads that stray response first -- a real, reproducible desync, not a simulated one -- and
    must be recognized by its mismatched `id` rather than misattributed to whichever request
    happens to be in flight.
    """

    async def run() -> None:
        async with await ReplWorker.spawn(lake_project_dir, ("Init",)) as worker:
            assert worker._process.stdin is not None
            worker._process.stdin.write(b'{"id": "injected", "body": "def unrelated : Nat := 1"}\n')
            await worker._process.stdin.drain()

            with pytest.raises(ReplProtocolError) as exc_info:
                await worker.check("def foo : Nat := 1")
            assert "injected" in str(exc_info.value)
            assert not worker.is_alive

    asyncio.run(run())


def test_default_timeout_is_spec_300_seconds() -> None:
    assert DEFAULT_TIMEOUT_MS == 300_000
