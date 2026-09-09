"""M1.9/gate-9 exit criterion: `memory_probe.py`'s measurement plumbing is correct -- these tests
assert the *code* is right (spawns real processes, sums the right pids, computes deltas
correctly), not any particular numeric RSS value, which is inherently noisy and not a
code-correctness property. See CLAUDE.md for the actual measured gate-9 results.

Uses `Init`-only imports throughout (not `Mathlib`) to keep these fast, unlike the real gate-9
measurement itself, which needs a real Mathlib base env to be a meaningful report.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from lean_agent_serv.memory_probe import (
    _process_group_rss_kib,
    measure_prelude_deltas,
    measure_worker_rss_kib,
)
from lean_agent_serv.repl import ReplWorker

LEANKERNEL_DIR = Path(__file__).resolve().parents[2] / "packages" / "leankernel"
LEANKERNEL_EXE = LEANKERNEL_DIR / ".lake" / "build" / "bin" / "leankernel"


@pytest.fixture(scope="session")
def lake_project_dir() -> Path:
    if not LEANKERNEL_EXE.exists():
        pytest.skip(
            f"{LEANKERNEL_EXE} not built; run `lake build` in {LEANKERNEL_DIR} first. "
            "CI always builds it before this suite runs (see .github/workflows/ci.yml)."
        )
    return LEANKERNEL_DIR


def test_process_group_rss_includes_this_process_itself() -> None:
    """Sanity check on `_process_group_rss_kib` independent of `leankernel` entirely: measuring
    this very test's own pid must include at least its own RSS, which is trivially nonzero.
    """
    assert _process_group_rss_kib(os.getpid()) > 0


def _pid_only_rss_kib(pid: int) -> int:
    """Like `_process_group_rss_kib`, but `pid` alone -- no children included. A separate,
    deliberately narrower helper for this one test, not something `memory_probe.py` itself needs.
    """
    output = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=True
    ).stdout
    return sum(int(line) for line in output.split())


def test_process_group_rss_sums_lake_and_its_leankernel_child(lake_project_dir: Path) -> None:
    """The whole reason `_process_group_rss_kib` exists rather than reading `ReplWorker.pid`'s
    own RSS directly: `lake exe`/`lake env` forks the real `leankernel` binary as a child rather
    than exec-replacing itself (M1.8.2's finding), so the worker's *real* memory footprint lives
    partly or wholly in that child. Confirmed here by comparing against `lake`'s own pid alone --
    the group total (including the child) must be strictly larger, not merely no smaller.
    """

    async def run() -> tuple[int, int]:
        worker = await ReplWorker.spawn(lake_project_dir, ("Init",))
        try:
            await worker.check("theorem t : True := trivial")
            lake_only = _pid_only_rss_kib(worker.pid)
            group_total = _process_group_rss_kib(worker.pid)
            return lake_only, group_total
        finally:
            await worker.close()

    lake_only, group_total = asyncio.run(run())
    assert group_total > lake_only > 0


def test_measure_worker_rss_kib_returns_a_plausible_value(lake_project_dir: Path) -> None:
    rss = asyncio.run(measure_worker_rss_kib(lake_project_dir, ("Init",)))
    # A real warm Lean process (even Init-only) is comfortably more than a trivial supervisor
    # process's footprint -- a loose lower bound (10 MiB) that would fail if this were somehow
    # only measuring `lake`'s own tiny wrapper process instead of the real worker.
    assert rss > 10 * 1024


def test_measure_prelude_deltas_computes_delta_as_with_minus_base(lake_project_dir: Path) -> None:
    results = asyncio.run(measure_prelude_deltas(lake_project_dir, ("Init",), ("Init",), repeats=1))
    assert set(results) == {"Init"}
    (result,) = results["Init"]
    assert result.delta_kib == result.with_prelude_rss_kib - result.base_rss_kib


def test_measure_prelude_deltas_respects_repeats(lake_project_dir: Path) -> None:
    results = asyncio.run(measure_prelude_deltas(lake_project_dir, ("Init",), ("Init",), repeats=2))
    assert len(results["Init"]) == 2
