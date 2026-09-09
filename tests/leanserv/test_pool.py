"""M1.8.3 exit criterion: `LeanReplPool` reuses warm workers, keeps concurrent checks against the
same base env from desynchronizing a shared process, evicts LRU-first under capacity pressure,
and never hands out a worker known to be dead -- against real spawned `leankernel serve`
processes (M1.8.2), never mocked.

Local dev / CI wiring matches `tests/leanserv/test_repl.py`: `lake build` in `packages/leankernel`
first, then `uv run pytest tests/leanserv`; the `lake_project_dir` fixture skips gracefully if
that build hasn't happened. No pytest-asyncio -- plain sync `def test_...` functions drive the
async `LeanReplPool` API via `asyncio.run`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from lean_agent_serv.pool import LeanReplPool, PoolConfig
from lean_agent_serv.repl import ReplTimeout

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


def test_acquire_reuses_released_worker(lake_project_dir: Path) -> None:
    async def run() -> None:
        pool = LeanReplPool(lake_project_dir, PoolConfig(max_total_workers=4))
        try:
            first = await pool.acquire("envA", ("Init",))
            await pool.release("envA", first)
            second = await pool.acquire("envA", ("Init",))
            assert second is first

            health = await pool.health()
            assert health.hit_rate == 0.5  # one miss (spawn), one hit (reuse)
        finally:
            await pool.aclose()

    asyncio.run(run())


def test_concurrent_acquire_for_same_key_spawns_distinct_workers(lake_project_dir: Path) -> None:
    """Two in-flight checks against the same base env must never share one process -- serve's
    strict one-request-one-response protocol (M1.8.1) desynchronizes under concurrent use.
    """

    async def run() -> None:
        pool = LeanReplPool(lake_project_dir, PoolConfig(max_total_workers=4, warm_per_base_env=4))
        try:
            first = await pool.acquire("envA", ("Init",))
            second = await pool.acquire("envA", ("Init",))  # first is still busy: must not reuse
            assert first is not second
            assert (await first.check("def a : Nat := 1")).ok
            assert (await second.check("def b : Nat := 2")).ok
        finally:
            await pool.aclose()

    asyncio.run(run())


def test_import_mismatch_for_same_key_raises(lake_project_dir: Path) -> None:
    async def run() -> None:
        pool = LeanReplPool(lake_project_dir, PoolConfig(max_total_workers=4))
        try:
            worker = await pool.acquire("envA", ("Init",))
            await pool.release("envA", worker)
            with pytest.raises(ValueError, match="must be stable"):
                await pool.acquire("envA", ("Mathlib",))
        finally:
            await pool.aclose()

    asyncio.run(run())


def test_lru_eviction_when_over_capacity(lake_project_dir: Path) -> None:
    """With room for only one warm worker, acquiring a second, different base env must evict the
    first (least-recently-used) idle worker rather than exceed the cap -- and the evicted worker
    must actually be dead, not merely forgotten by the pool.
    """

    async def run() -> None:
        pool = LeanReplPool(lake_project_dir, PoolConfig(max_total_workers=1))
        try:
            env_a_worker = await pool.acquire("envA", ("Init",))
            await pool.release("envA", env_a_worker)
            assert pool.total_workers == 1

            env_b_worker = await pool.acquire("envB", ("Init",))
            assert pool.total_workers == 1  # envA's idle worker was evicted to make room
            assert not env_a_worker.is_alive

            await pool.release("envB", env_b_worker)

            # envA has nothing warm left -- this must be a fresh spawn (a miss), not the evicted
            # worker resurrected. A miss never increases the hit count while it does increase the
            # total, so hit_rate can only stay the same (both zero) or drop -- never rise.
            health_before = await pool.health()
            env_a_again = await pool.acquire("envA", ("Init",))
            health_after = await pool.health()
            assert env_a_again is not env_a_worker
            assert health_after.hit_rate <= health_before.hit_rate
            await pool.release("envA", env_a_again)
        finally:
            await pool.aclose()

    asyncio.run(run())


def test_crashed_worker_is_not_returned_to_idle(lake_project_dir: Path) -> None:
    async def run() -> None:
        pool = LeanReplPool(lake_project_dir, PoolConfig(max_total_workers=4))
        try:
            worker = await pool.acquire("envA", ("Init",))
            with pytest.raises(ReplTimeout):
                await worker.check("partial def spin : IO Unit := spin\n#eval spin", timeout_ms=500)
            assert not worker.is_alive

            await pool.release("envA", worker)  # must discard, not re-idle, a dead worker
            health = await pool.health()
            assert health.idle_workers == 0

            replacement = await pool.acquire("envA", ("Init",))
            assert replacement is not worker
            assert replacement.is_alive
            await pool.release("envA", replacement)
        finally:
            await pool.aclose()

    asyncio.run(run())


def test_worker_context_manager_releases_on_exception(lake_project_dir: Path) -> None:
    async def run() -> None:
        pool = LeanReplPool(lake_project_dir, PoolConfig(max_total_workers=4))
        try:
            with pytest.raises(RuntimeError):
                async with pool.worker("envA", ("Init",)) as worker:
                    assert (await worker.check("def a : Nat := 1")).ok
                    raise RuntimeError("simulated caller-side failure, not a worker crash")

            health = await pool.health()
            assert health.idle_workers == 1
            assert health.busy_workers == 0
        finally:
            await pool.aclose()

    asyncio.run(run())


def test_aclose_closes_every_worker(lake_project_dir: Path) -> None:
    async def run() -> None:
        pool = LeanReplPool(lake_project_dir, PoolConfig(max_total_workers=4))
        idle_worker = await pool.acquire("envA", ("Init",))
        await pool.release("envA", idle_worker)
        busy_worker = await pool.acquire("envB", ("Init",))

        await pool.aclose()

        assert not idle_worker.is_alive
        assert not busy_worker.is_alive
        health = await pool.health()
        assert health.idle_workers == 0
        assert health.busy_workers == 0

    asyncio.run(run())
