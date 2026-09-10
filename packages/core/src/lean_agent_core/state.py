"""Obligation state machine and the §1.1 acceptance predicate (spec §6.4).

**The database is the enforcer; this module is the decider.** Every status transition is a
`SECURITY DEFINER` function in `deploy/grants.sql`, each re-deriving its own precondition from
committed rows, because `app` holds no `UPDATE` privilege on `obligation.status` at all. Nothing
here can move an obligation on its own, and that is the point: spec §5.5/§6.4's principle is that
workers request a transition and observe the outcome, never transcribe one.

What this module adds on top is the part a worker genuinely needs and the database cannot supply:
which transition to *ask for*, given an attempt's outcome. `TRANSITIONS` is that table, and it is
deliberately a mirror of the SQL rather than a second source of truth. `tests/db/test_state.py`
holds the two together in both directions against the real functions: every pair the table calls
legal must actually land where it says, and no pair it calls illegal may change the status. (Not
"must raise" -- several transitions are deliberate no-ops when they arrive late or twice, and an
earlier version of that test demanded an exception from all of them and was simply wrong about the
design.) A mirror checked against the original is a convenience; an unchecked one is a liability.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lean_agent_core.enums import ObligationStatus, VerdictKind
from lean_agent_core.orm import Obligation, ObligationEdge, Verdict


class ObligationOutcome(StrEnum):
    """The labelled arrows out of `in_progress` in spec §6.4's diagram, plus the two that leave
    `decomposed`. One outcome per way an attempt can end, named for what happened rather than for
    the status it produces -- `RETRYABLE_FAILURE` and `INFRA_ERROR` both lead back to `open` but
    are emphatically not the same event, and collapsing them is spec's own named "most common way
    these systems silently report a wrong pass rate".
    """

    PROVED = "proved"
    DECOMPOSED = "decomposed"
    RETRYABLE_FAILURE = "retryable_failure"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INFRA_ERROR = "infra_error"
    GROUPS_EXHAUSTED = "groups_exhausted"


#: Which statuses each outcome may be applied from, and where it leads. Read directly off spec
#: §6.4's diagram.
#:
#: `PROVED` is reachable from `decomposed` as well as `in_progress` because a parent's reassembly
#: is an ordinary attempt against the parent -- spec is explicit that there is "no 'all children
#: proved implies parent proved' shortcut", so reassembly flows through `mark_proved` like
#: anything else.
TRANSITIONS: dict[ObligationOutcome, tuple[frozenset[ObligationStatus], ObligationStatus]] = {
    ObligationOutcome.PROVED: (
        frozenset(
            {ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS, ObligationStatus.DECOMPOSED}
        ),
        ObligationStatus.PROVED,
    ),
    ObligationOutcome.DECOMPOSED: (
        frozenset({ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS}),
        ObligationStatus.DECOMPOSED,
    ),
    ObligationOutcome.RETRYABLE_FAILURE: (
        frozenset({ObligationStatus.IN_PROGRESS}),
        ObligationStatus.OPEN,
    ),
    ObligationOutcome.INFRA_ERROR: (
        frozenset({ObligationStatus.IN_PROGRESS}),
        ObligationStatus.OPEN,
    ),
    ObligationOutcome.BUDGET_EXHAUSTED: (
        frozenset({ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS}),
        ObligationStatus.FAILED,
    ),
    ObligationOutcome.GROUPS_EXHAUSTED: (
        frozenset({ObligationStatus.DECOMPOSED}),
        ObligationStatus.BLOCKED,
    ),
}

#: Statuses no outcome leads out of. `abandoned` is here rather than in `TRANSITIONS` because spec
#: §6.4's diagram has no arrow producing it: the enum defines it (§5.2) but nothing in the control
#: loop abandons an obligation, and inventing a rule for when to would be design, not
#: implementation. Whatever cancels a run will need one, and should add it here with its own
#: function rather than reusing `mark_failed`, which means something different.
TERMINAL_STATUSES: frozenset[ObligationStatus] = frozenset(
    {ObligationStatus.PROVED, ObligationStatus.FAILED, ObligationStatus.ABANDONED}
)


def is_legal(current: ObligationStatus, outcome: ObligationOutcome) -> bool:
    """Whether §6.4's diagram has an arrow labelled `outcome` leaving `current`.

    Advisory, and specifically about arrows rather than about what the database will accept. A
    `True` does not promise the transition will happen: the SQL function still checks the rest of
    its own precondition (a verdict, a budget, a live group), and a concurrent attempt may have
    moved the obligation already. A `False` does not promise an exception either -- several
    transitions are deliberate no-ops rather than errors when they arrive late or twice
    (`mark_decomposed` on an already-decomposed parent, `release_obligation` on one another
    attempt has since proved). What `False` does guarantee, and what `tests/db/test_state.py`
    checks against the real functions, is that asking cannot *change* the status.
    """
    return current in TRANSITIONS[outcome][0]


def resulting_status(outcome: ObligationOutcome) -> ObligationStatus:
    return TRANSITIONS[outcome][1]


def acceptance_predicate(verdict: Verdict, obligation: Obligation) -> bool:
    """Spec §1.1's core invariant, as a pure function: an obligation is provable by this verdict
    only when it *links*, *replays*, *audits*, and seal integrity holds.

    This is the same predicate `mark_proved` enforces in SQL, and the duplication is intentional
    and one-directional. This copy exists so a worker can tell whether calling `mark_proved` is
    warranted (and report honestly when it isn't) without provoking an exception; it is never a
    substitute for the SQL, which is the only thing that can actually write `proved`. If the two
    ever disagree, the SQL is right by construction -- it is the one holding the privilege.
    """
    return (
        verdict.kind == VerdictKind.PROVED
        and verdict.link_ok
        and verdict.replay_ok
        and verdict.axiom_audit_ok
        and verdict.sealed_olean_sha_observed is not None
        and verdict.sealed_olean_sha_observed == obligation.sealed_olean_sha
    )


@dataclass(frozen=True)
class GroupProgress:
    """One decomposition group's children, summarized (spec §4.5). `group_id` identifies the
    group; a parent may hold several competing ones."""

    group_id: uuid.UUID
    total: int
    proved: int
    dead: int

    @property
    def all_proved(self) -> bool:
        """Every child proved. Necessary for the parent's reassembly to be worth attempting, and
        deliberately not sufficient for the parent to be proved: spec §4.6 requires the reassembly
        term itself to link, replay and audit against the parent's *sealed* goal, which is what
        catches children that are individually correct but do not compose.
        """
        return self.total > 0 and self.proved == self.total

    @property
    def is_dead(self) -> bool:
        return self.dead > 0


async def group_progress(
    session_factory: async_sessionmaker[AsyncSession], obligation_id: uuid.UUID
) -> list[GroupProgress]:
    """Per-group child counts for one parent, the input to both "is a reassembly worth trying"
    and "is this parent blocked".

    A child counts as dead in `failed`, `blocked` or `abandoned` -- the three statuses nothing
    leads out of toward `proved`. `open`/`in_progress`/`decomposed` children are simply not done
    yet, and a group holding one is still live.
    """
    dead_statuses = (ObligationStatus.FAILED, ObligationStatus.BLOCKED, ObligationStatus.ABANDONED)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ObligationEdge.group_id, Obligation.status)
                .join(Obligation, Obligation.id == ObligationEdge.child_id)
                .where(ObligationEdge.parent_id == obligation_id)
            )
        ).all()

    by_group: dict[uuid.UUID, list[ObligationStatus]] = {}
    for group_id, status in rows:
        by_group.setdefault(group_id, []).append(status)
    return [
        GroupProgress(
            group_id=group_id,
            total=len(statuses),
            proved=sum(1 for s in statuses if s == ObligationStatus.PROVED),
            dead=sum(1 for s in statuses if s in dead_statuses),
        )
        for group_id, statuses in by_group.items()
    ]


def every_group_is_dead(groups: list[GroupProgress]) -> bool:
    """Spec §6.4's "every group has a failed child" condition for `decomposed -> blocked`.

    False for a parent with no groups at all: that obligation is not blocked, it is simply not
    decomposed, and treating "no groups" as "all groups dead" would block every parent the moment
    it was looked at.
    """
    return len(groups) > 0 and all(group.is_dead for group in groups)


class ObligationStateMachine:
    """Thin async wrapper over the `SECURITY DEFINER` transition functions.

    Thin on purpose. Each method is one function call with no logic of its own, because any
    precondition this class checked instead of the database would be a check an attacker (or a
    bug) could route around -- `app` reaches Postgres directly. What the class buys is a typed,
    named surface for the six arrows, so a control loop calls `retryable_failure(...)` rather than
    assembling SQL, and so the outcome taxonomy stays visible in Python where policies live.

    Takes a session factory rather than a session, matching `VerificationCacheStore`/`VerdictWriter`
    (M1.8.4): constructing a role-scoped connection is a deployment concern.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _call(self, sql: str, params: dict[str, object]) -> None:
        async with self._session_factory() as session:
            await session.execute(text(sql), params)
            await session.commit()

    async def mark_proved(self, obligation_id: uuid.UUID, attempt_id: uuid.UUID) -> None:
        """`-> proved`, the only path to it (spec §5.5). Raises if the §1.1 predicate is not
        satisfied by the named attempt's verdict; idempotent for an already-proved obligation,
        since concurrent attempts on one obligation are normal."""
        await self._call(
            "SELECT mark_proved(:obligation, :attempt)",
            {"obligation": obligation_id, "attempt": attempt_id},
        )

    async def mark_decomposed(self, obligation_id: uuid.UUID, group_id: uuid.UUID) -> None:
        """`-> decomposed`. The group's edges must already be inserted -- this asserts children
        exist rather than promising they will."""
        await self._call(
            "SELECT mark_decomposed(:obligation, :group)",
            {"obligation": obligation_id, "group": group_id},
        )

    async def retryable_failure(self, obligation_id: uuid.UUID) -> None:
        """`in_progress -> open`, charging one attempt. Spec §6.4's "errors / timeout" arrow: the
        obligation is still provable, this attempt just did not do it."""
        await self._call(
            "SELECT release_obligation(:obligation, true)", {"obligation": obligation_id}
        )

    async def infra_error(self, obligation_id: uuid.UUID) -> None:
        """`in_progress -> open`, charging nothing. An infra failure is not evidence about the
        content, so it must not consume the obligation's proof budget."""
        await self._call(
            "SELECT release_obligation(:obligation, false)", {"obligation": obligation_id}
        )

    async def budget_exhausted(self, obligation_id: uuid.UUID) -> None:
        """`-> failed`. Refused unless `spent_attempts >= budget_attempts` on the committed row."""
        await self._call("SELECT mark_failed(:obligation)", {"obligation": obligation_id})

    async def groups_exhausted(self, obligation_id: uuid.UUID) -> None:
        """`decomposed -> blocked`. Refused while any decomposition group can still succeed."""
        await self._call("SELECT mark_blocked(:obligation)", {"obligation": obligation_id})
