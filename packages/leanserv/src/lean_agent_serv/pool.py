"""Base-env-keyed pool of `ReplWorker`s (M1.8.2) with LRU eviction across base environments --
spec §6.2's worker model: "One REPL process per worker, keyed by `base_env_digest`, LRU over base
environments."

`base_env_key` is a plain `str` throughout this module, not the `bytea` digest spec's `base_env`
table stores -- the pool doesn't touch Postgres or know what a digest means, only that it's a
stable identifier a caller uses to mean "the same base environment"; a caller with a `bytes`
digest passes `digest.hex()`. Which *imports* that key means is supplied by the caller at
`acquire` time rather than looked up here, since resolving a digest to a recipe is a `base_env`
table lookup -- a concern for whatever higher-level code (the control loop, `api.py`) already has
a database session, not for a pool that only ever spawns and reuses processes.

Mutual exclusion, not just reuse, is the reason this module exists rather than callers spawning
their own `ReplWorker`s directly: `LeanKernel.Serve`'s wire protocol (M1.8.1) is strictly one
request in, one response out, in order -- sending two concurrent `check`s to the *same* process
desynchronizes it exactly the way `ReplProtocolError` detects (M1.8.2's own test proves this is a
real, reachable failure, not a hypothetical one). `acquire`/`release` give each in-flight check
sole ownership of one worker; `warm_per_base_env` is how many workers may exist concurrently for
one base env specifically so concurrent requests against the same base env don't serialize on one
process.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from lean_agent_serv.repl import ReplWorker


@dataclass(frozen=True)
class PoolConfig:
    """Spec Appendix B's `[leanserv]` config, the pool's own slice of it. `max_total_workers`
    corresponds to spec §6.2's derived `LRU capacity` (`floor(RAM / memory_cap_gib) - reserve`) --
    that arithmetic is a deployment-time computation over real hardware, not this module's job;
    the pool only ever enforces whatever total it's given.
    """

    max_total_workers: int
    warm_per_base_env: int = 4


@dataclass(frozen=True)
class PoolHealth:
    """Spec §6.2's `/v1/health`: "Pool occupancy, LRU hit rate, warm counts per base env"."""

    idle_workers: int
    busy_workers: int
    warm_by_base_env: dict[str, int]
    hit_rate: float


class LeanReplPool:
    """Owns zero or more `ReplWorker`s per `base_env_key`, up to `PoolConfig.warm_per_base_env`
    each and `PoolConfig.max_total_workers` overall. Not itself an async context manager (a pool
    is a long-lived resource an application owns for its whole run, not a single `async with`
    scope) -- call `aclose()` explicitly during shutdown.
    """

    def __init__(self, lake_project_dir: Path, config: PoolConfig) -> None:
        self._lake_project_dir = lake_project_dir
        self._config = config
        self._idle: dict[str, list[ReplWorker]] = defaultdict(list)
        self._busy: dict[str, set[ReplWorker]] = defaultdict(set)
        self._imports: dict[str, tuple[str, ...]] = {}
        self._last_used_at: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    @property
    def total_workers(self) -> int:
        return sum(len(w) for w in self._idle.values()) + sum(len(w) for w in self._busy.values())

    async def acquire(self, base_env_key: str, imports: tuple[str, ...]) -> ReplWorker:
        """Check out one worker for `base_env_key`, reusing an idle one if available. `imports`
        must be the same every time a given `base_env_key` is used -- a mismatch is a caller bug
        (two different recipes claiming the same identity), not something to silently paper over
        by picking one, so it raises rather than reusing or replacing the recorded imports.
        """
        async with self._lock:
            recorded = self._imports.setdefault(base_env_key, imports)
            if recorded != imports:
                raise ValueError(
                    f"base_env_key {base_env_key!r} was first used with imports {recorded!r}, "
                    f"now requested with {imports!r} -- a base env's imports must be stable "
                    "for its key's whole lifetime"
                )
            self._last_used_at[base_env_key] = time.monotonic()

            idle_list = self._idle[base_env_key]
            while idle_list:
                worker = idle_list.pop()
                if worker.is_alive:
                    self._busy[base_env_key].add(worker)
                    self._hits += 1
                    return worker
                # Found a dead worker sitting idle (crashed between release and this acquire,
                # e.g. reaped by something else) -- drop it and keep looking rather than handing
                # out something already known to be unusable.

            self._misses += 1
            if self.total_workers >= self._config.max_total_workers:
                await self._evict_one_locked(exclude_key=base_env_key)
            worker = await ReplWorker.spawn(self._lake_project_dir, imports)
            self._busy[base_env_key].add(worker)
            return worker

    async def release(self, base_env_key: str, worker: ReplWorker) -> None:
        """Return a worker acquired for `base_env_key`. A crashed worker (`is_alive` false) is
        closed rather than kept warm -- `ReplWorker.is_alive` already reflects every crash path
        M1.8.2 defines, so there is no separate "was this check successful" signal to thread
        through here: a worker that merely answered `ok=False` (an ordinary elaboration failure)
        is still alive and still perfectly reusable.
        """
        async with self._lock:
            self._busy[base_env_key].discard(worker)
            if worker.is_alive and len(self._idle[base_env_key]) < self._config.warm_per_base_env:
                self._idle[base_env_key].append(worker)
                return
        await worker.close()

    @asynccontextmanager
    async def worker(
        self, base_env_key: str, imports: tuple[str, ...]
    ) -> AsyncIterator[ReplWorker]:
        """Convenience wrapper around `acquire`/`release` for the common single-check case --
        releases even if the body raises. A caller implementing spec §6.2's `/v1/check_batch`
        ("N bodies, one base env; amortizes worker acquisition") should call `acquire`/`release`
        directly instead, once, around every body in the batch rather than paying acquisition
        overhead per body.
        """
        acquired = await self.acquire(base_env_key, imports)
        try:
            yield acquired
        finally:
            await self.release(base_env_key, acquired)

    async def _evict_one_locked(self, *, exclude_key: str) -> None:
        """Evict the single idle worker belonging to the least-recently-used base env other than
        `exclude_key` (the one about to be served) -- spec's "LRU over base environments", at the
        granularity of one freed slot per call, since that's all a caller blocked on `acquire`
        actually needs. Must be called with `self._lock` already held. A no-op if every base env
        with idle capacity is `exclude_key` itself or has no idle worker to give up -- the new
        worker is then simply spawned over the soft cap rather than blocking indefinitely, since
        there is nothing left this pool can evict.
        """
        candidates = sorted(
            (key for key, workers in self._idle.items() if key != exclude_key and workers),
            key=lambda key: self._last_used_at.get(key, 0.0),
        )
        if not candidates:
            return
        lru_key = candidates[0]
        victim = self._idle[lru_key].pop()
        await victim.close()

    async def health(self) -> PoolHealth:
        async with self._lock:
            warm_by_base_env = {key: len(workers) for key, workers in self._idle.items() if workers}
            idle = sum(len(workers) for workers in self._idle.values())
            busy = sum(len(workers) for workers in self._busy.values())
            total_requests = self._hits + self._misses
            hit_rate = self._hits / total_requests if total_requests else 0.0
        return PoolHealth(
            idle_workers=idle,
            busy_workers=busy,
            warm_by_base_env=warm_by_base_env,
            hit_rate=hit_rate,
        )

    async def aclose(self) -> None:
        """Close every worker, idle or busy, and forget all pool state. Only meant for shutdown --
        closing a worker out from under an in-flight `check()` call is the caller's problem to
        avoid (e.g. by draining before calling this), not something this method guards against.
        """
        async with self._lock:
            workers = [w for lst in self._idle.values() for w in lst]
            workers += [w for busy_set in self._busy.values() for w in busy_set]
            self._idle.clear()
            self._busy.clear()
        await asyncio.gather(*(w.close() for w in workers), return_exceptions=True)
