"""The control loop (spec §6.4): claim an attempt, run it under a heartbeat, commit its outcome.

This is where M2.2's state machine and M2.3's scheduler meet. The loop itself owns three things
nothing else does -- the idle backoff, the lease/heartbeat lifetime around one attempt, and the
commit path that turns "what the attempt did" into "what happens to the obligation".

**What runs inside the loop is injected, not defined here.** Spec §6.4 looks up a `Policy` by
`attempt.policy_id` and runs it; spec Appendix A's `Policy` yields `Action`s that an executor
performs. Both of those are M2.5, and defining a placeholder `Policy` protocol now would only be
something M2.5 replaces. So the loop takes an `AttemptRunner` -- one callable from a claimed
attempt to an `AttemptResult` -- and M2.5 supplies the real one built over `Policy`/`Action`. The
seam is deliberate and is the only part of §6.4 not implemented here.

Run-level budgets (tokens, wallclock, kernel-seconds across a whole run) live in spec's own
`budget.py` and are not here either: this loop records what an attempt spent and enforces the one
budget that governs *scheduling* -- `obligation.budget_attempts` -- because that is the one the
state machine's `failed` arrow depends on. The others need a policy that actually spends tokens.

**Known gap, scheduled for Phase 4: nothing re-claims a `decomposed` parent for reassembly, and
nothing calls `mark_blocked`.** `claim_attempt` selects `status = 'open'`, so once a parent
decomposes it is never picked up again -- and spec §6.4 is explicit that a parent becomes proved
only by presenting its own verdict. Both halves are pinned by `xfail(strict=True)` tests in
`tests/db/test_state.py`, which fail loudly the moment either is closed.

It is not fixed here because it cannot be *validated* here: nothing in `packages/` inserts an
`obligation_edge`, so no component produces a `decomposed` obligation at all. The only policy that
decomposes is `DecomposeAndConquer`, which spec §8 places in Phase 4 -- whose exit criterion is
precisely the multi-`sorry` end-to-end run this design needs to be checked against. CLAUDE.md
carries the four open questions and a provisional recommendation (promotion rather than widening
this claim, which would perturb the Phase 3 "bit-identical baseline" exit criterion).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lean_agent_core.enums import AttemptStatus
from lean_agent_core.scheduler import (
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_LEASE,
    ClaimedAttempt,
    HeartbeatThread,
    claim_attempt,
)
from lean_agent_core.state import ObligationOutcome, ObligationStateMachine

logger = logging.getLogger(__name__)


class InfraError(Exception):
    """Spec §6.4's own exception: something about the *infrastructure* failed, so nothing was
    learned about the obligation and no budget may be charged.

    Declared explicitly by whatever hit it -- a crashed Lean worker, an unreachable model
    endpoint. An *undeclared* exception escaping a runner is deliberately not treated as one; see
    `_run_attempt` for why that distinction matters more than it looks.
    """


@dataclass(frozen=True)
class AttemptSpend:
    """What one attempt consumed. Recorded on the `attempt` row and rolled up into the obligation,
    which is what makes a run's cost reconstructible from its attempts rather than only from
    whatever the reporting layer remembered to add up."""

    tokens_in: int = 0
    tokens_out: int = 0
    kernel_ms: int = 0


@dataclass(frozen=True)
class AttemptResult:
    """What an attempt did, in the terms the obligation state machine understands.

    `outcome` is deliberately not free-form: it is one of §6.4's labelled arrows, so a runner
    cannot invent a transition the diagram has no edge for. `group_id` is required for
    `DECOMPOSED` (the group whose edges the runner inserted) and meaningless otherwise.
    """

    outcome: ObligationOutcome
    spend: AttemptSpend = AttemptSpend()
    group_id: uuid.UUID | None = None
    detail: str | None = None


#: One attempt's work. M2.5 supplies the real implementation over `Policy`/`Action`; the loop only
#: needs "given this claimed attempt, tell me what happened".
AttemptRunner = Callable[[ClaimedAttempt], Awaitable[AttemptResult]]


class Backoff:
    """Exponential backoff for the idle case, reset on every successful claim.

    Idle is the common case for a worker pool sized for peak, so a fixed short sleep would make
    an idle fleet a steady stream of `claim_attempt` calls against the one table every busy worker
    also needs. Jitter is deliberately absent here and belongs with whatever runs many workers on
    one host -- adding it per worker without knowing the fleet size just makes the cadence harder
    to reason about.
    """

    def __init__(self, initial: float = 0.1, maximum: float = 5.0, factor: float = 2.0) -> None:
        self._initial = initial
        self._maximum = maximum
        self._factor = factor
        self._current = initial

    def next(self) -> float:
        delay = self._current
        self._current = min(self._current * self._factor, self._maximum)
        return delay

    def reset(self) -> None:
        self._current = self._initial


async def _finish_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    attempt: ClaimedAttempt,
    status: AttemptStatus,
    spend: AttemptSpend,
    wallclock_ms: int,
) -> None:
    """Close out the `attempt` row and roll its spend into the obligation.

    Both in one transaction: an attempt recorded as finished whose cost never reached the
    obligation would understate that obligation's spend permanently, and nothing later
    recomputes it.

    Every column touched here is one `app` holds directly -- table-level `UPDATE` on `attempt`,
    and column-level `UPDATE (spent_tokens, spent_kernel_ms, ...)` on `obligation`. The status
    transition is a separate call precisely because `obligation.status` is not among them.

    The attempt's status is only overwritten while it is still `claimed`/`running`. An attempt the
    reaper already marked `expired` stays `expired`: that is a fact about what happened to this
    worker, and a loop that finished late and stamped `failed` over it would erase the only
    evidence that a worker was flapping. The spend is recorded either way -- tokens and kernel
    time were genuinely consumed no matter who ended up owning the attempt.
    """
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE attempt SET status = CASE WHEN status IN ('claimed', 'running') "
                "THEN CAST(:status AS attempt_status) ELSE status END, "
                "finished_at = COALESCE(finished_at, now()), lease_owner = NULL, "
                "tokens_in = :tin, tokens_out = :tout, kernel_ms = :kms, wallclock_ms = :wms "
                "WHERE id = :id"
            ),
            {
                "status": status.value,
                "tin": spend.tokens_in,
                "tout": spend.tokens_out,
                "kms": spend.kernel_ms,
                "wms": wallclock_ms,
                "id": attempt.attempt_id,
            },
        )
        await session.execute(
            text(
                "UPDATE obligation SET spent_tokens = spent_tokens + :tokens, "
                "spent_kernel_ms = spent_kernel_ms + :kms, updated_at = now() WHERE id = :id"
            ),
            {
                "tokens": spend.tokens_in + spend.tokens_out,
                "kms": spend.kernel_ms,
                "id": attempt.obligation_id,
            },
        )
        await session.commit()


async def _attempt_budget_exhausted(
    session_factory: async_sessionmaker[AsyncSession], obligation_id: uuid.UUID
) -> bool:
    """Spec §6.4: quota is "checked coarsely [at claim] and exactly at commit". This is the exact
    check -- read after the charge has landed, so it sees the attempt that just finished."""
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT spent_attempts >= budget_attempts FROM obligation WHERE id = :id"),
                {"id": obligation_id},
            )
        ).scalar_one()
    return bool(row)


class Worker:
    """One control-loop worker (spec §6.4).

    Owns no policy and no Lean process: it claims work, keeps the lease alive while a runner does
    the work, and commits the outcome through the two boundaries that enforce anything
    (`ObligationStateMachine` for status, the `app` grants for everything else).
    """

    def __init__(
        self,
        *,
        worker_id: str,
        session_factory: async_sessionmaker[AsyncSession],
        heartbeat_engine: Engine,
        runner: AttemptRunner,
        policy_id: str,
        policy_config_hash: bytes,
        lease: timedelta = DEFAULT_LEASE,
        heartbeat_every: timedelta = DEFAULT_HEARTBEAT_INTERVAL,
        backoff: Backoff | None = None,
        eligible_tenants: list[uuid.UUID] | None = None,
    ) -> None:
        self.worker_id = worker_id
        #: Spec §6.4's `$eligible_tenants`, passed through to every claim. `None` claims from any
        #: tenant, as a deployment's workers do; an evaluation run passes its own tenant, because
        #: the claim is global by design and would otherwise spend the prover on whatever else
        #: happens to be open in the same database (M3.12).
        self._eligible_tenants = eligible_tenants
        self._sessions = session_factory
        self._heartbeat_engine = heartbeat_engine
        self._runner = runner
        self._policy_id = policy_id
        self._policy_config_hash = policy_config_hash
        self._lease = lease
        self._heartbeat_every = heartbeat_every
        self._backoff = backoff or Backoff()
        self._state = ObligationStateMachine(session_factory)

    async def run_forever(self, shutdown: asyncio.Event) -> None:
        """Claim and run attempts until `shutdown` is set.

        Shutdown is checked between attempts, never during one: interrupting a claimed attempt
        would leave it to be reaped, discarding real work to save at most one lease. A worker
        draining is worth up to one attempt's wallclock.
        """
        while not shutdown.is_set():
            if not await self.run_once():
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=self._backoff.next())
                except TimeoutError:
                    continue

    async def run_once(self) -> bool:
        """Claim one attempt and see it through. Returns whether there was anything to do.

        `False` means the queue is empty right now -- the ordinary idle case, which the caller
        backs off on. Exposed separately from `run_forever` because a single deterministic step is
        what a test (and a one-shot CLI invocation) actually wants.
        """
        claimed = await claim_attempt(
            self._sessions,
            worker_id=self.worker_id,
            policy_id=self._policy_id,
            policy_config_hash=self._policy_config_hash,
            lease=self._lease,
            eligible_tenants=self._eligible_tenants,
        )
        if claimed is None:
            return False
        self._backoff.reset()
        await self._execute(claimed)
        return True

    async def _execute(self, claimed: ClaimedAttempt) -> None:
        started = time.monotonic()
        with HeartbeatThread(
            self._heartbeat_engine,
            claimed.attempt_id,
            every=self._heartbeat_every,
            lease=self._lease,
        ) as heartbeat:
            result, infra_error = await self._run_attempt(claimed)
        wallclock_ms = int((time.monotonic() - started) * 1000)

        if infra_error is not None:
            await self._commit_infra_error(claimed, infra_error, wallclock_ms)
            return
        assert result is not None
        await self._commit(claimed, result, wallclock_ms, lost_lease=heartbeat.lost_lease)

    async def _run_attempt(
        self, claimed: ClaimedAttempt
    ) -> tuple[AttemptResult | None, InfraError | None]:
        """Run the injected runner, separating declared infrastructure failures from everything
        else.

        A declared `InfraError` is spec's unbudgeted outcome. An **undeclared** exception is
        charged as an ordinary failed attempt instead, which is a deliberate refinement of §6.4
        rather than an oversight: treating every crash as unbudgeted means an obligation that
        reliably crashes the policy is retried forever at no cost, occupying a worker indefinitely
        and never reaching `failed`. A policy that consistently blows up on one obligation is
        telling you something about that obligation, and the budget is the mechanism that
        eventually stops asking.
        """
        try:
            return await self._runner(claimed), None
        except InfraError as exc:
            logger.warning("attempt %s hit infrastructure trouble: %s", claimed.attempt_id, exc)
            return None, exc
        except Exception as exc:  # charged, not swallowed -- see this method's docstring
            logger.exception("attempt %s raised", claimed.attempt_id)
            return AttemptResult(
                outcome=ObligationOutcome.RETRYABLE_FAILURE,
                detail=f"{type(exc).__name__}: {exc}",
            ), None

    async def _commit_infra_error(
        self, claimed: ClaimedAttempt, error: InfraError, wallclock_ms: int
    ) -> None:
        """Spec's `commit_infra_error`: "no budget charged". The spend recorded is whatever was
        genuinely consumed before the failure -- real tokens and kernel time do not become
        unspent because the run ended badly -- but the *attempt* is not charged, so the
        obligation's schedulability is untouched."""
        del error
        await _finish_attempt(
            self._sessions, claimed, AttemptStatus.INFRA_ERROR, AttemptSpend(), wallclock_ms
        )
        await self._state.infra_error(claimed.obligation_id)

    async def _commit(
        self, claimed: ClaimedAttempt, result: AttemptResult, wallclock_ms: int, *, lost_lease: bool
    ) -> None:
        """Record the attempt, then ask for the transition its outcome implies.

        Order matters: `_finish_attempt` charges spend and `retryable_failure` charges the attempt,
        and the budget check that follows has to see both.

        A lost lease -- the reaper got here first, because this worker stopped heartbeating long
        enough to look dead -- splits by outcome rather than discarding everything:

        * `PROVED` and `DECOMPOSED` are still committed. A verified proof is verified regardless of
          who holds a lease, `mark_proved` re-checks the §1.1 predicate against the verdict itself,
          and the decomposition's children are already in the database. Throwing away real,
          independently-validated work because of a scheduling timeout would be pure loss.
        * Everything else is dropped. The reaper already returned the obligation to `open`, and
          another worker may hold it now; charging it for this attempt would take budget from work
          that is no longer ours to charge.
        """
        status = (
            AttemptStatus.SUCCEEDED
            if result.outcome in (ObligationOutcome.PROVED, ObligationOutcome.DECOMPOSED)
            else AttemptStatus.FAILED
        )
        await _finish_attempt(self._sessions, claimed, status, result.spend, wallclock_ms)

        if lost_lease and result.outcome not in (
            ObligationOutcome.PROVED,
            ObligationOutcome.DECOMPOSED,
        ):
            logger.warning(
                "attempt %s lost its lease; dropping %s rather than charging an obligation "
                "another worker may now hold",
                claimed.attempt_id,
                result.outcome,
            )
            return

        match result.outcome:
            case ObligationOutcome.PROVED:
                await self._state.mark_proved(claimed.obligation_id, claimed.attempt_id)
            case ObligationOutcome.DECOMPOSED:
                if result.group_id is None:
                    raise ValueError("DECOMPOSED result carries no group_id")
                await self._state.mark_decomposed(claimed.obligation_id, result.group_id)
            case ObligationOutcome.RETRYABLE_FAILURE:
                await self._state.retryable_failure(claimed.obligation_id)
                await self._fail_if_out_of_budget(claimed.obligation_id)
            case ObligationOutcome.INFRA_ERROR:
                await self._state.infra_error(claimed.obligation_id)
            case _:
                # BUDGET_EXHAUSTED and GROUPS_EXHAUSTED are conclusions the loop draws from the
                # database, never outcomes a runner reports: a runner has no way to know whether
                # the budget is spent or whether every competing group is dead.
                raise ValueError(f"a runner may not report {result.outcome}")

    async def _fail_if_out_of_budget(self, obligation_id: uuid.UUID) -> None:
        """Spec §6.4's "budget exhausted -> failed" arrow, applied right after the charge lands.

        Left to the next claim instead, an obligation with no budget would sit `open` forever:
        `claim_attempt` filters it out, so nothing would ever pick it up to notice it was done.
        """
        if await _attempt_budget_exhausted(self._sessions, obligation_id):
            await self._state.budget_exhausted(obligation_id)
