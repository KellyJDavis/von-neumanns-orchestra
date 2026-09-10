"""M2.4 exit criterion: the control loop (spec §6.4) against a real PostgreSQL 16, as the real
`app` role -- claim, heartbeat, run, commit, with M2.2's state machine and M2.3's scheduler
underneath.

The runner is a stub throughout, and deliberately so: what is under test is the loop's *commit
path*, which is the part that decides what happens to an obligation. A real policy arrives in
M2.5 and plugs into the same seam.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import timedelta

import pytest
from lean_agent_core.enums import ObligationStatus
from lean_agent_core.scheduler import ClaimedAttempt
from lean_agent_core.state import ObligationOutcome
from lean_agent_core.worker import (
    AttemptResult,
    AttemptSpend,
    Backoff,
    InfraError,
    Worker,
)
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

WorkerBody = Callable[[Worker], Awaitable[None]]


class Fixture:
    """A committed run plus an obligation factory, through the admin connection."""

    def __init__(self, engine: Engine, run_id: uuid.UUID, base_env_digest: bytes) -> None:
        self._engine = engine
        self.run_id = run_id
        self.base_env_digest = base_env_digest

    def obligation(
        self,
        *,
        status: ObligationStatus = ObligationStatus.OPEN,
        budget_attempts: int = 8,
        spent_attempts: int = 0,
        sealed_olean_sha: bytes | None = None,
    ) -> uuid.UUID:
        obligation_id = uuid.uuid4()
        with self._engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO obligation (id, run_id, base_env_digest, goal_digest, "
                    "sealed_olean_sha, goal_src, decl_name, status, budget_attempts, "
                    "spent_attempts) VALUES (:id, :run, :base_env, :gd, :sealed, 'src', 'decl', "
                    "CAST(:status AS obligation_status), :budget, :spent)"
                ),
                {
                    "id": obligation_id,
                    "run": self.run_id,
                    "base_env": self.base_env_digest,
                    "gd": f"goal-{uuid.uuid4()}".encode(),
                    "sealed": sealed_olean_sha or f"sealed-{uuid.uuid4()}".encode(),
                    "status": status.value,
                    "budget": budget_attempts,
                    "spent": spent_attempts,
                },
            )
            conn.commit()
        return obligation_id

    def child_of(self, parent: uuid.UUID, group: uuid.UUID) -> uuid.UUID:
        child = uuid.uuid4()
        with self._engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO obligation (id, run_id, base_env_digest, goal_digest, "
                    "sealed_olean_sha, goal_src, decl_name, depth) VALUES (:id, :run, :base_env, "
                    ":gd, :sealed, 'src', 'decl', 1)"
                ),
                {
                    "id": child,
                    "run": self.run_id,
                    "base_env": self.base_env_digest,
                    "gd": f"goal-{uuid.uuid4()}".encode(),
                    "sealed": f"sealed-{uuid.uuid4()}".encode(),
                },
            )
            conn.execute(
                text(
                    "INSERT INTO obligation_edge (parent_id, child_id, group_id, role) "
                    "VALUES (:p, :c, :g, 'subgoal')"
                ),
                {"p": parent, "c": child, "g": group},
            )
            conn.commit()
        return child

    def write_verdict(self, attempt_id: uuid.UUID, obligation_id: uuid.UUID) -> None:
        """A `proved` verdict satisfying the §1.1 predicate, written through the admin connection
        -- in production only `leanserv` may, which is exactly why the loop cannot fabricate one.
        """
        with self._engine.connect() as conn:
            sealed = conn.execute(
                text("SELECT sealed_olean_sha FROM obligation WHERE id = :id"),
                {"id": obligation_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO verdict (attempt_id, obligation_id, kind, link_ok, replay_ok, "
                    "axiom_audit_ok, sealed_olean_sha_observed, elapsed_ms, toolchain_rev, "
                    "mathlib_rev) VALUES (:a, :o, 'proved', true, true, true, :sealed, 1, "
                    "'v4.33.1', 'deadbeef')"
                ),
                {"a": attempt_id, "o": obligation_id, "sealed": sealed},
            )
            conn.commit()

    def reap(self) -> int:
        with self._engine.connect() as conn:
            reaped = conn.execute(text("SELECT reap_expired_attempts(100)")).scalar_one()
            conn.commit()
        return int(reaped)

    def expire_lease(self, attempt_id: uuid.UUID) -> None:
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
    base_env_digest = f"worker-test-{uuid.uuid4()}".encode()
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
def worker(
    app_async_database_url: str, app_database_url: str
) -> Callable[[Callable[[ClaimedAttempt], Awaitable[AttemptResult]], WorkerBody], None]:
    """Builds a `Worker` around `runner` and runs `body` against it, with both engines created and
    disposed inside one `asyncio.run` (M2.2's asyncpg loop-affinity finding).

    Two engines because the heartbeat genuinely needs a *synchronous* one on its own thread --
    that is the design, not an accommodation.
    """

    def run(runner: Callable[[ClaimedAttempt], Awaitable[AttemptResult]], body: WorkerBody) -> None:
        async def main() -> None:
            async_engine = create_async_engine(app_async_database_url)
            sync_engine = create_engine(app_database_url)
            try:
                await body(
                    Worker(
                        worker_id="w1",
                        session_factory=async_sessionmaker(async_engine, expire_on_commit=False),
                        heartbeat_engine=sync_engine,
                        runner=runner,
                        policy_id="stub",
                        policy_config_hash=b"cfg",
                        heartbeat_every=timedelta(milliseconds=50),
                    )
                )
            finally:
                sync_engine.dispose()
                await async_engine.dispose()

        asyncio.run(main())

    return run


def _returns(result: AttemptResult) -> Callable[[ClaimedAttempt], Awaitable[AttemptResult]]:
    async def runner(claimed: ClaimedAttempt) -> AttemptResult:
        del claimed
        return result

    return runner


WorkerRunner = Callable[[Callable[[ClaimedAttempt], Awaitable[AttemptResult]], WorkerBody], None]


def test_run_once_is_false_when_there_is_nothing_to_claim(
    fx: Fixture, worker: WorkerRunner
) -> None:
    """The idle signal the backoff depends on. `False`, not an exception -- an empty queue must be
    distinguishable from a broken one."""
    fx.obligation(status=ObligationStatus.PROVED)

    async def body(w: Worker) -> None:
        assert await w.run_once() is False

    worker(_returns(AttemptResult(outcome=ObligationOutcome.RETRYABLE_FAILURE)), body)


def test_a_proved_outcome_needs_a_real_verdict(fx: Fixture, worker: WorkerRunner) -> None:
    """The loop cannot promote an obligation by *saying* it proved one. `mark_proved` re-checks
    the §1.1 predicate against a `verdict` row only `leanserv` can write, so a runner reporting
    PROVED with nothing behind it fails loudly rather than quietly marking work done.
    """
    obligation = fx.obligation()

    async def body(w: Worker) -> None:
        with pytest.raises(Exception, match="acceptance predicate not satisfied"):
            await w.run_once()

    worker(_returns(AttemptResult(outcome=ObligationOutcome.PROVED)), body)
    assert fx.row("obligation", "status::text", obligation) == ("in_progress",)


def test_a_proved_outcome_with_a_verdict_marks_the_obligation_proved(
    fx: Fixture, worker: WorkerRunner
) -> None:
    obligation = fx.obligation()

    async def runner(claimed: ClaimedAttempt) -> AttemptResult:
        # Stands in for what a real attempt does: ask leanserv to check, which writes the verdict.
        fx.write_verdict(claimed.attempt_id, claimed.obligation_id)
        return AttemptResult(
            outcome=ObligationOutcome.PROVED, spend=AttemptSpend(tokens_in=10, kernel_ms=250)
        )

    async def body(w: Worker) -> None:
        assert await w.run_once() is True

    worker(runner, body)
    assert fx.row("obligation", "status::text", obligation) == ("proved",)
    spent_tokens, spent_kernel = fx.row("obligation", "spent_tokens, spent_kernel_ms", obligation)
    assert (spent_tokens, spent_kernel) == (10, 250)


def test_a_retryable_failure_reopens_the_obligation_and_charges_one_attempt(
    fx: Fixture, worker: WorkerRunner
) -> None:
    obligation = fx.obligation(budget_attempts=8)

    async def body(w: Worker) -> None:
        assert await w.run_once() is True

    worker(_returns(AttemptResult(outcome=ObligationOutcome.RETRYABLE_FAILURE)), body)
    status, spent = fx.row("obligation", "status::text, spent_attempts", obligation)
    assert (status, spent) == ("open", 1)


def test_the_last_attempt_leaves_the_obligation_failed_not_open(
    fx: Fixture, worker: WorkerRunner
) -> None:
    """Spec §6.4's "budget exhausted -> failed", applied at commit rather than left for later.

    This is the test that would catch leaving it to the next claim: `claim_attempt` filters out
    obligations with no budget, so an exhausted obligation parked in `open` is never picked up
    again and never reaches `failed` -- it just sits there looking schedulable forever.
    """
    obligation = fx.obligation(budget_attempts=1, spent_attempts=0)

    async def body(w: Worker) -> None:
        assert await w.run_once() is True

    worker(_returns(AttemptResult(outcome=ObligationOutcome.RETRYABLE_FAILURE)), body)
    status, spent = fx.row("obligation", "status::text, spent_attempts", obligation)
    assert (status, spent) == ("failed", 1)


def test_an_infra_error_reopens_the_obligation_without_charging(
    fx: Fixture, worker: WorkerRunner
) -> None:
    """Spec's `commit_infra_error`: "no budget charged". A flaky node must not consume an
    obligation's proof budget, or infrastructure trouble silently becomes a lower pass rate."""
    obligation = fx.obligation()

    async def runner(claimed: ClaimedAttempt) -> AttemptResult:
        del claimed
        raise InfraError("lean worker crashed")

    async def body(w: Worker) -> None:
        assert await w.run_once() is True

    worker(runner, body)
    status, spent = fx.row("obligation", "status::text, spent_attempts", obligation)
    assert (status, spent) == ("open", 0)


def test_an_undeclared_exception_is_charged_as_a_failed_attempt(
    fx: Fixture, worker: WorkerRunner
) -> None:
    """A deliberate refinement of §6.4, and the test that pins it down.

    Treating *every* exception as `InfraError` means an obligation that reliably crashes the
    policy is retried forever at no cost, occupying a worker indefinitely and never reaching
    `failed`. Only a declared `InfraError` is unbudgeted; anything else is a failed attempt, so
    the budget eventually stops asking.
    """
    obligation = fx.obligation(budget_attempts=8)

    async def runner(claimed: ClaimedAttempt) -> AttemptResult:
        del claimed
        raise ValueError("a bug in the policy, not the infrastructure")

    async def body(w: Worker) -> None:
        assert await w.run_once() is True

    worker(runner, body)
    status, spent = fx.row("obligation", "status::text, spent_attempts", obligation)
    assert (status, spent) == ("open", 1)


def test_a_decomposed_outcome_records_the_group(fx: Fixture, worker: WorkerRunner) -> None:
    obligation = fx.obligation()
    group = uuid.uuid4()

    async def runner(claimed: ClaimedAttempt) -> AttemptResult:
        # A real runner seals the children and inserts their edges before reporting DECOMPOSED;
        # `mark_decomposed` refuses a group with no children, so the order is not optional.
        fx.child_of(claimed.obligation_id, group)
        return AttemptResult(outcome=ObligationOutcome.DECOMPOSED, group_id=group)

    async def body(w: Worker) -> None:
        assert await w.run_once() is True

    worker(runner, body)
    assert fx.row("obligation", "status::text", obligation) == ("decomposed",)


def test_a_runner_may_not_report_a_conclusion_the_loop_draws(
    fx: Fixture, worker: WorkerRunner
) -> None:
    """`BUDGET_EXHAUSTED` and `GROUPS_EXHAUSTED` are read out of the database, never reported: a
    runner has no way to know whether the budget is spent or every competing group is dead."""
    fx.obligation()

    async def body(w: Worker) -> None:
        with pytest.raises(ValueError, match="may not report"):
            await w.run_once()

    worker(_returns(AttemptResult(outcome=ObligationOutcome.BUDGET_EXHAUSTED)), body)


def test_a_lost_lease_drops_a_failure_rather_than_charging_another_workers_obligation(
    fx: Fixture, worker: WorkerRunner
) -> None:
    """The reaper got here first, so the obligation is back in the pool and may belong to someone
    else. Charging it for this attempt would take budget from work that is no longer ours."""
    obligation = fx.obligation(spent_attempts=0)
    attempts: list[uuid.UUID] = []

    async def runner(claimed: ClaimedAttempt) -> AttemptResult:
        attempts.append(claimed.attempt_id)
        # Stand in for the worker having looked dead long enough to be reaped: expire the lease
        # and reap it, exactly as the real reaper would, while this attempt is still running.
        fx.expire_lease(claimed.attempt_id)
        fx.reap()
        # Then wait long enough for at least one heartbeat to land and discover the loss --
        # `lost_lease` is set by a beat, so a test that skipped this would assert on a flag
        # nothing had yet had a chance to set.
        await asyncio.sleep(0.2)
        return AttemptResult(outcome=ObligationOutcome.RETRYABLE_FAILURE)

    async def body(w: Worker) -> None:
        await w.run_once()

    worker(runner, body)
    # The obligation was never charged: the loop saw the lost lease and dropped the outcome. The
    # reaper had already returned it to `open`, so it is schedulable again for whoever takes it.
    status, spent = fx.row("obligation", "status::text, spent_attempts", obligation)
    assert (status, spent) == ("open", 0)
    # And the attempt still reads `expired`, not `failed`: the reaper's account of what happened
    # to this worker survives the loop finishing late. Also proves the lease loss was genuinely
    # detected rather than the test passing for some unrelated reason.
    assert fx.row("attempt", "status::text", attempts[0]) == ("expired",)


def test_run_forever_stops_on_shutdown_and_backs_off_when_idle(
    fx: Fixture, worker: WorkerRunner
) -> None:
    """Shutdown is checked between attempts, and the idle wait is interruptible -- a draining
    worker must not sit out a full backoff interval before noticing."""
    del fx

    async def body(w: Worker) -> None:
        shutdown = asyncio.Event()
        task = asyncio.create_task(w.run_forever(shutdown))
        await asyncio.sleep(0.05)
        shutdown.set()
        await asyncio.wait_for(task, timeout=2.0)

    worker(_returns(AttemptResult(outcome=ObligationOutcome.RETRYABLE_FAILURE)), body)


def test_the_loop_drains_a_queue_of_independent_obligations(
    fx: Fixture, worker: WorkerRunner
) -> None:
    """The whole loop, repeatedly, over a real queue: three obligations with different fates,
    driven only by `run_once` until it reports idle.

    Independent obligations rather than a parent/child DAG, deliberately: nothing re-claims a
    `decomposed` parent for reassembly yet (see `worker.py`'s "Known seam"), so a DAG test would
    have to encode a guess at a decision M2.5 makes. This exercises what is actually built.
    """
    proves = fx.obligation()
    retries = fx.obligation(budget_attempts=2)
    crashes = fx.obligation(budget_attempts=1)

    async def runner(claimed: ClaimedAttempt) -> AttemptResult:
        if claimed.obligation_id == proves:
            fx.write_verdict(claimed.attempt_id, claimed.obligation_id)
            return AttemptResult(
                outcome=ObligationOutcome.PROVED, spend=AttemptSpend(tokens_in=5, tokens_out=7)
            )
        if claimed.obligation_id == crashes:
            raise InfraError("the lean worker died again")
        return AttemptResult(outcome=ObligationOutcome.RETRYABLE_FAILURE)

    async def body(w: Worker) -> None:
        # Bounded: an infra_error never charges, so `crashes` is re-claimable forever by design
        # and an unbounded drain would not terminate. That is the intended behaviour, not a bug --
        # a permanently broken node should not consume proof budget.
        for _ in range(12):
            if not await w.run_once():
                break

    worker(runner, body)

    assert fx.row("obligation", "status::text", proves) == ("proved",)
    assert fx.row("obligation", "spent_tokens", proves) == (12,)
    # Two attempts allowed, two spent, so it ends `failed` rather than parked `open` where nothing
    # would ever look at it again.
    assert fx.row("obligation", "status::text, spent_attempts", retries) == ("failed", 2)
    # Never charged, however many times it crashed -- still schedulable, budget intact.
    assert fx.row("obligation", "status::text, spent_attempts", crashes) == ("open", 0)


def test_backoff_grows_to_a_cap_and_resets() -> None:
    backoff = Backoff(initial=0.1, maximum=0.4, factor=2.0)
    assert [backoff.next() for _ in range(5)] == [0.1, 0.2, 0.4, 0.4, 0.4]
    backoff.reset()
    assert backoff.next() == 0.1
