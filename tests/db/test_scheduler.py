"""M2.3 exit criterion: claim, lease, heartbeat and reaper (spec §6.4) against a real
PostgreSQL 16, driven as the real `app` role.

The two tests worth reading first are the ones that check spec's *reasons* rather than its
mechanics: `test_two_concurrent_claims_never_take_the_same_obligation` (why `SKIP LOCKED` is
there) and `test_heartbeat_keeps_beating_while_the_event_loop_is_blocked` (why the heartbeat is a
thread and not an asyncio task). Both would pass vacuously against an implementation that had the
shape right and the substance wrong, so both are written to fail if the substance is missing.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import timedelta

import pytest
from lean_agent_core.enums import ObligationStatus
from lean_agent_core.scheduler import (
    ClaimedAttempt,
    HeartbeatThread,
    claim_attempt,
    reap_expired_attempts,
)
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

#: What `sessions` runs: one coroutine taking a session factory bound to the `app` role.
SessionBody = Callable[[async_sessionmaker[AsyncSession]], Awaitable[None]]


class Fixture:
    """A committed run plus obligation/attempt factories, built through the admin connection --
    the harness needs privileges the code under test must not have."""

    def __init__(self, engine: Engine, run_id: uuid.UUID, base_env_digest: bytes) -> None:
        self._engine = engine
        self.run_id = run_id
        self.base_env_digest = base_env_digest

    def obligation(
        self,
        *,
        status: ObligationStatus = ObligationStatus.OPEN,
        priority: float = 0.0,
        depth: int = 0,
        budget_attempts: int = 8,
        spent_attempts: int = 0,
    ) -> uuid.UUID:
        obligation_id = uuid.uuid4()
        with self._engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO obligation (id, run_id, base_env_digest, goal_digest, "
                    "sealed_olean_sha, goal_src, decl_name, status, priority, depth, "
                    "budget_attempts, spent_attempts) VALUES (:id, :run, :base_env, :gd, :sealed, "
                    "'src', 'decl', CAST(:status AS obligation_status), :priority, :depth, "
                    ":budget, :spent)"
                ),
                {
                    "id": obligation_id,
                    "run": self.run_id,
                    "base_env": self.base_env_digest,
                    "gd": f"goal-{uuid.uuid4()}".encode(),
                    "sealed": f"sealed-{uuid.uuid4()}".encode(),
                    "status": status.value,
                    "priority": priority,
                    "depth": depth,
                    "budget": budget_attempts,
                    "spent": spent_attempts,
                },
            )
            conn.commit()
        return obligation_id

    def set_run_status(self, status: str) -> None:
        with self._engine.connect() as conn:
            conn.execute(
                text("UPDATE run SET status = :s WHERE id = :id"),
                {"s": status, "id": self.run_id},
            )
            conn.commit()

    def expire_lease(self, attempt_id: uuid.UUID) -> None:
        """Backdate a lease rather than waiting one out. The reaper's condition is
        `lease_expires_at < now()` evaluated by Postgres, so moving the timestamp exercises exactly
        the same comparison a real expiry would, without the test sleeping for a minute."""
        with self._engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE attempt SET lease_expires_at = now() - interval '1 second' "
                    "WHERE id = :id"
                ),
                {"id": attempt_id},
            )
            conn.commit()

    def row(self, table: str, columns: str, row_id: uuid.UUID) -> tuple[object, ...]:
        with self._engine.connect() as conn:
            return tuple(
                conn.execute(
                    text(f"SELECT {columns} FROM {table} WHERE id = :id"), {"id": row_id}
                ).one()
            )


@pytest.fixture
def fx(admin_engine: Engine) -> Iterator[Fixture]:
    base_env_digest = f"sched-test-{uuid.uuid4()}".encode()
    run_id = uuid.uuid4()
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO base_env (digest, recipe, toolchain_rev, mathlib_rev) "
                "VALUES (:digest, '{}', 'v4.33.1', 'deadbeef')"
            ),
            {"digest": base_env_digest},
        )
        conn.execute(
            text(
                "INSERT INTO run (id, tenant_id, base_env_digest, status, manifest, manifest_hash)"
                " VALUES (:id, :tenant, :base_env, 'running', '{}', :mh)"
            ),
            {
                "id": run_id,
                "tenant": uuid.uuid4(),
                "base_env": base_env_digest,
                "mh": b"manifest",
            },
        )
        conn.commit()
    yield Fixture(admin_engine, run_id, base_env_digest)
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": run_id})
        conn.execute(
            text("DELETE FROM base_env WHERE digest = :digest"), {"digest": base_env_digest}
        )
        conn.commit()


@pytest.fixture
def sessions(app_async_database_url: str) -> Callable[[SessionBody], None]:
    """Runs `body` against an `app`-role session factory, disposing the engine inside the same
    `asyncio.run` -- an asyncpg engine is bound to the loop that created it, and disposing from a
    different one fails once the pool is actually holding a connection (M2.2's finding)."""

    def run(body: SessionBody) -> None:
        async def main() -> None:
            engine = create_async_engine(app_async_database_url)
            try:
                await body(async_sessionmaker(engine, expire_on_commit=False))
            finally:
                await engine.dispose()

        asyncio.run(main())

    return run


def _claim(
    factory: async_sessionmaker[AsyncSession], worker: str = "w1"
) -> Awaitable[ClaimedAttempt | None]:
    return claim_attempt(
        factory, worker_id=worker, policy_id="null-agent", policy_config_hash=b"cfg"
    )


def test_claim_marks_the_obligation_and_opens_an_attempt(
    fx: Fixture, sessions: Callable[[SessionBody], None]
) -> None:
    obligation = fx.obligation()
    claimed: list[ClaimedAttempt] = []

    async def body(factory: async_sessionmaker[AsyncSession]) -> None:
        result = await _claim(factory)
        assert result is not None
        claimed.append(result)

    sessions(body)
    (result,) = claimed
    assert result.obligation_id == obligation
    assert result.run_id == fx.run_id
    assert fx.row("obligation", "status::text", obligation) == ("in_progress",)

    status, owner, lease_expires, heartbeat = fx.row(
        "attempt", "status::text, lease_owner, lease_expires_at, heartbeat_at", result.attempt_id
    )
    assert (status, owner) == ("claimed", "w1")
    # Both stamped at claim time: an attempt whose lease was set but whose heartbeat was NULL
    # would look to the reaper like one that never checked in.
    assert lease_expires is not None
    assert heartbeat is not None


def test_claim_returns_none_when_nothing_is_schedulable(
    fx: Fixture, sessions: Callable[[SessionBody], None]
) -> None:
    """The idle case is `None`, not an exception -- the control loop backs off on it, and an
    exception would make an empty queue indistinguishable from a broken one."""
    fx.obligation(status=ObligationStatus.PROVED)
    fx.obligation(status=ObligationStatus.OPEN, budget_attempts=2, spent_attempts=2)

    async def body(factory: async_sessionmaker[AsyncSession]) -> None:
        assert await _claim(factory) is None

    sessions(body)


def test_claim_skips_obligations_of_a_run_that_is_not_running(
    fx: Fixture, sessions: Callable[[SessionBody], None]
) -> None:
    fx.obligation()
    fx.set_run_status("paused")

    async def body(factory: async_sessionmaker[AsyncSession]) -> None:
        assert await _claim(factory) is None

    sessions(body)


def test_claim_orders_by_priority_then_depth_then_age(
    fx: Fixture, sessions: Callable[[SessionBody], None]
) -> None:
    """Spec's `ORDER BY o.priority DESC, o.depth ASC, o.created_at ASC`. Depth ascending matters:
    shallower obligations unblock more work, so a deep subgoal should not outrank its own
    grandparent's sibling merely by existing."""
    fx.obligation(priority=1.0, depth=5)
    shallow = fx.obligation(priority=1.0, depth=0)
    fx.obligation(priority=0.5, depth=0)
    high = fx.obligation(priority=9.0, depth=3)

    order: list[uuid.UUID] = []

    async def body(factory: async_sessionmaker[AsyncSession]) -> None:
        for _ in range(2):
            result = await _claim(factory)
            assert result is not None
            order.append(result.obligation_id)

    sessions(body)
    assert order == [high, shallow]


def test_two_concurrent_claims_never_take_the_same_obligation(
    fx: Fixture, app_async_database_url: str
) -> None:
    """Why `FOR UPDATE ... SKIP LOCKED` is in the claim at all.

    Eight workers claim concurrently against four obligations. Without `SKIP LOCKED` they would
    serialize on the highest-priority row and hand the same obligation out twice (or block); with
    it, each takes a different one and the rest come back idle. Asserting the claimed obligation
    ids are *distinct* is the property that would break under either failure.

    Concurrency here is real -- eight separate connections through `asyncio.gather` -- rather than
    two sequential calls, which would pass against a completely unlocked implementation.
    """
    obligations = {fx.obligation() for _ in range(4)}

    async def main() -> list[ClaimedAttempt | None]:
        engine = create_async_engine(app_async_database_url, pool_size=8, max_overflow=4)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            return list(await asyncio.gather(*(_claim(factory, f"w{i}") for i in range(8))))
        finally:
            await engine.dispose()

    results = asyncio.run(main())
    claimed = [r.obligation_id for r in results if r is not None]
    assert len(claimed) == len(set(claimed)), "an obligation was handed to two workers"
    assert set(claimed) <= obligations
    assert len(claimed) == 4


def test_heartbeat_extends_the_lease_and_stamps_the_time(
    fx: Fixture, sessions: Callable[[SessionBody], None], app_database_url: str
) -> None:
    obligation = fx.obligation()
    claimed: list[ClaimedAttempt] = []

    async def body(factory: async_sessionmaker[AsyncSession]) -> None:
        result = await _claim(factory)
        assert result is not None
        claimed.append(result)

    sessions(body)
    (attempt,) = claimed
    del obligation

    (before,) = fx.row("attempt", "lease_expires_at", attempt.attempt_id)
    engine = create_engine(app_database_url)
    try:
        heartbeat = HeartbeatThread(engine, attempt.attempt_id, lease=timedelta(seconds=600))
        assert heartbeat.beat_once() is True
    finally:
        engine.dispose()
    (after,) = fx.row("attempt", "lease_expires_at", attempt.attempt_id)
    assert after > before  # type: ignore[operator]


def test_heartbeat_reports_a_lost_lease_rather_than_raising(
    fx: Fixture, sessions: Callable[[SessionBody], None], app_database_url: str
) -> None:
    """If the reaper got there first, the worker's work is orphaned. The thread records that and
    keeps quiet -- it has no way to interrupt whatever the worker is doing, and raising inside a
    background thread would only be swallowed."""
    fx.obligation()
    claimed: list[ClaimedAttempt] = []

    async def body(factory: async_sessionmaker[AsyncSession]) -> None:
        result = await _claim(factory)
        assert result is not None
        claimed.append(result)
        fx.expire_lease(result.attempt_id)
        assert await reap_expired_attempts(factory) == 1

    sessions(body)
    (attempt,) = claimed

    engine = create_engine(app_database_url)
    try:
        heartbeat = HeartbeatThread(engine, attempt.attempt_id)
        assert heartbeat.beat_once() is False
        assert heartbeat.lost_lease is True
    finally:
        engine.dispose()


def test_heartbeat_keeps_beating_while_the_event_loop_is_blocked(
    fx: Fixture, sessions: Callable[[SessionBody], None], app_database_url: str
) -> None:
    """The entire reason the heartbeat is a thread rather than an asyncio task.

    Spec: "a blocking tokenizer call or CPU-bound serialization inside a policy would otherwise
    starve it and get a live worker reaped." This reproduces exactly that -- the caller blocks
    synchronously, the way a real policy does inside a tokenizer -- and asserts beats still land
    in the database. An asyncio heartbeat would record zero beats here and the worker would be
    reaped while perfectly healthy.
    """
    fx.obligation()
    claimed: list[ClaimedAttempt] = []

    async def body(factory: async_sessionmaker[AsyncSession]) -> None:
        result = await _claim(factory)
        assert result is not None
        claimed.append(result)

    sessions(body)
    (attempt,) = claimed

    engine = create_engine(app_database_url, pool_size=4)
    try:

        async def blocking_work() -> None:
            with HeartbeatThread(
                engine,
                attempt.attempt_id,
                every=timedelta(milliseconds=50),
                lease=timedelta(seconds=600),
            ) as heartbeat:
                # Blocking the loop is the whole scenario under test, so ruff's ASYNC251 ("async
                # functions should not call time.sleep") is correct in general and inverted here:
                # `asyncio.sleep` would yield to the loop and let an asyncio heartbeat run, which
                # is exactly what this test exists to rule out.
                time.sleep(0.5)  # noqa: ASYNC251
                assert heartbeat.beats >= 3, (
                    f"only {heartbeat.beats} beats landed while the loop was blocked -- "
                    "the heartbeat is not running independently of the event loop"
                )
                assert heartbeat.lost_lease is False

        asyncio.run(blocking_work())
    finally:
        engine.dispose()

    (heartbeat_at,) = fx.row("attempt", "heartbeat_at", attempt.attempt_id)
    assert heartbeat_at is not None


def test_reaper_expires_the_attempt_and_reopens_the_obligation_without_charging(
    fx: Fixture, sessions: Callable[[SessionBody], None]
) -> None:
    """Spec: "Lease expiry means *the worker is gone*, never *the attempt took too long*."

    So the obligation goes back to `open` with `spent_attempts` untouched. Charging it would let a
    node that keeps dying quietly consume every obligation's budget -- the same silent pass-rate
    depression `infra_error` exists to prevent.
    """
    obligation = fx.obligation(spent_attempts=2)
    claimed: list[ClaimedAttempt] = []

    async def body(factory: async_sessionmaker[AsyncSession]) -> None:
        result = await _claim(factory)
        assert result is not None
        claimed.append(result)
        fx.expire_lease(result.attempt_id)
        assert await reap_expired_attempts(factory) == 1

    sessions(body)
    (attempt,) = claimed

    assert fx.row("attempt", "status::text, lease_owner", attempt.attempt_id) == ("expired", None)
    status, spent = fx.row("obligation", "status::text, spent_attempts", obligation)
    assert status == "open"
    assert spent == 2


def test_reaper_leaves_a_live_lease_alone(
    fx: Fixture, sessions: Callable[[SessionBody], None]
) -> None:
    """A reaper that swept live attempts would be far worse than none at all -- it would discard
    healthy work at random."""
    obligation = fx.obligation()

    async def body(factory: async_sessionmaker[AsyncSession]) -> None:
        assert await _claim(factory) is not None
        assert await reap_expired_attempts(factory) == 0

    sessions(body)
    assert fx.row("obligation", "status::text", obligation) == ("in_progress",)


def test_reaped_obligation_can_be_claimed_again(
    fx: Fixture, sessions: Callable[[SessionBody], None]
) -> None:
    """The point of reaping: work a dead worker was holding becomes available again, to a
    different worker, without having cost the obligation anything."""
    obligation = fx.obligation()
    attempts: list[ClaimedAttempt | None] = []

    async def body(factory: async_sessionmaker[AsyncSession]) -> None:
        first = await _claim(factory, "w1")
        assert first is not None
        attempts.append(first)
        fx.expire_lease(first.attempt_id)
        await reap_expired_attempts(factory)
        attempts.append(await _claim(factory, "w2"))

    sessions(body)
    first, second = attempts
    assert first is not None and second is not None
    assert second.obligation_id == obligation
    # A *new* attempt, not the reaped one resurrected -- retrying means a new attempt row, which
    # is also what keeps `verdict.attempt_id` usable as a primary key.
    assert second.attempt_id != first.attempt_id
    assert fx.row("attempt", "lease_owner", second.attempt_id) == ("w2",)
