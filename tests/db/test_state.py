"""M2.2 exit criterion: the obligation state machine (spec §6.4) against a real PostgreSQL 16,
driven as the real `app` role -- never as an admin connection, since the entire design rests on
`app` being unable to write `obligation.status` by any route other than these functions.

Two things are being verified, and the second is the one that keeps the design honest:

1. Each transition does what spec §6.4's diagram says, and each *refuses* when its own
   precondition is not met -- computed by the function from committed rows, not taken from the
   caller.
2. `lean_agent_core.state.TRANSITIONS`, the Python mirror of that diagram, agrees with the SQL.
   `test_python_transition_table_agrees_with_sql` drives every (status, outcome) pair the mirror
   calls illegal against the real function and confirms it is refused. An unchecked mirror of an
   enforcement boundary is a liability, not a convenience.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator

import pytest
from lean_agent_core.enums import ObligationStatus
from lean_agent_core.orm import Obligation, Verdict
from lean_agent_core.state import (
    TRANSITIONS,
    GroupProgress,
    ObligationOutcome,
    ObligationStateMachine,
    acceptance_predicate,
    every_group_is_dead,
    group_progress,
    is_legal,
)
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

#: Every function under test raises through asyncpg as a `DBAPIError` wrapping a
#: `RaiseError`; the message is the `RAISE EXCEPTION` text.
Refused = DBAPIError

#: What `machine` runs: one coroutine taking the state machine under test.
MachineBody = Callable[[ObligationStateMachine], Awaitable[None]]


class Fixture:
    """A committed run plus a factory for obligations in it. Built through the admin connection
    because setting up arbitrary starting statuses is exactly what `app` is not allowed to do --
    the harness needs a privilege the code under test must not have.
    """

    def __init__(self, engine: Engine, run_id: uuid.UUID, base_env_digest: bytes) -> None:
        self._engine = engine
        self.run_id = run_id
        self.base_env_digest = base_env_digest

    def obligation(
        self,
        *,
        status: ObligationStatus = ObligationStatus.IN_PROGRESS,
        goal_digest: bytes | None = None,
        depth: int = 0,
        budget_attempts: int = 8,
        spent_attempts: int = 0,
        sealed_olean_sha: bytes | None = None,
    ) -> uuid.UUID:
        obligation_id = uuid.uuid4()
        with self._engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO obligation (id, run_id, base_env_digest, goal_digest, "
                    "sealed_olean_sha, goal_src, decl_name, status, depth, budget_attempts, "
                    "spent_attempts) VALUES (:id, :run, :base_env, :gd, :sealed, 'src', 'decl', "
                    "CAST(:status AS obligation_status), :depth, :budget, :spent)"
                ),
                {
                    "id": obligation_id,
                    "run": self.run_id,
                    "base_env": self.base_env_digest,
                    "gd": goal_digest or f"goal-{uuid.uuid4()}".encode(),
                    "sealed": sealed_olean_sha or f"sealed-{uuid.uuid4()}".encode(),
                    "status": status.value,
                    "depth": depth,
                    "budget": budget_attempts,
                    "spent": spent_attempts,
                },
            )
            conn.commit()
        return obligation_id

    def edge(
        self, parent: uuid.UUID, child: uuid.UUID, group: uuid.UUID, role: str = "subgoal"
    ) -> None:
        with self._engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO obligation_edge (parent_id, child_id, group_id, role) "
                    "VALUES (:p, :c, :g, :r)"
                ),
                {"p": parent, "c": child, "g": group, "r": role},
            )
            conn.commit()

    def status_of(self, obligation_id: uuid.UUID) -> str:
        with self._engine.connect() as conn:
            return str(
                conn.execute(
                    text("SELECT status::text FROM obligation WHERE id = :id"),
                    {"id": obligation_id},
                ).scalar_one()
            )

    def spent_attempts(self, obligation_id: uuid.UUID) -> int:
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT spent_attempts FROM obligation WHERE id = :id"),
                    {"id": obligation_id},
                ).scalar_one()
            )


@pytest.fixture
def fx(admin_engine: Engine) -> Iterator[Fixture]:
    base_env_digest = f"state-test-{uuid.uuid4()}".encode()
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
def machine(app_async_database_url: str) -> Callable[[MachineBody], None]:
    """Runs `body` against a state machine on a freshly-built engine, and disposes that engine
    inside the *same* `asyncio.run`.

    Creating the engine in one `asyncio.run` and disposing it in another fails outright with
    "Event loop is closed" once the pool is actually holding a connection -- asyncpg connections
    are bound to the loop that created them (M1.8.4's finding for the same reason). An earlier
    version of this fixture disposed afterwards and passed for every short test while failing the
    one that made enough round trips to keep a connection checked out.
    """

    def run(body: MachineBody) -> None:
        async def main() -> None:
            engine = create_async_engine(app_async_database_url)
            try:
                sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
                await body(ObligationStateMachine(sessionmaker))
            finally:
                await engine.dispose()

        asyncio.run(main())

    return run


def test_decomposed_requires_children(fx: Fixture, machine: Callable[[MachineBody], None]) -> None:
    """Spec's own gloss on `decomposed` is "children exist". A parent parked there with no group
    would never be claimed again and would have nothing to reassemble -- silently abandoned."""
    parent = fx.obligation()
    group = uuid.uuid4()

    async def body(sm: ObligationStateMachine) -> None:
        with pytest.raises(Refused):
            await sm.mark_decomposed(parent, group)

    machine(body)
    assert fx.status_of(parent) == "in_progress"


def test_decomposed_succeeds_once_the_group_exists(
    fx: Fixture, machine: Callable[[MachineBody], None]
) -> None:
    parent = fx.obligation()
    child = fx.obligation(status=ObligationStatus.OPEN, depth=1)
    group = uuid.uuid4()
    fx.edge(parent, child, group)

    async def body(sm: ObligationStateMachine) -> None:
        await sm.mark_decomposed(parent, group)
        # A second, competing group arriving for an already-decomposed parent is normal, not an
        # error (spec §4.5: "a parent may hold several competing decompositions").
        await sm.mark_decomposed(parent, group)

    machine(body)
    assert fx.status_of(parent) == "decomposed"


def test_retryable_failure_charges_an_attempt(
    fx: Fixture, machine: Callable[[MachineBody], None]
) -> None:
    obligation = fx.obligation(spent_attempts=2)

    async def body(sm: ObligationStateMachine) -> None:
        await sm.retryable_failure(obligation)

    machine(body)
    assert fx.status_of(obligation) == "open"
    assert fx.spent_attempts(obligation) == 3


def test_infra_error_does_not_charge_an_attempt(
    fx: Fixture, machine: Callable[[MachineBody], None]
) -> None:
    """Spec's most-repeated principle: `infra_error` is a first-class *unbudgeted* outcome. A
    crashed worker must not consume the obligation's proof budget, or a flaky node quietly
    converts into a lower reported pass rate."""
    obligation = fx.obligation(spent_attempts=2)

    async def body(sm: ObligationStateMachine) -> None:
        await sm.infra_error(obligation)

    machine(body)
    assert fx.status_of(obligation) == "open"
    assert fx.spent_attempts(obligation) == 2


def test_release_does_not_resurrect_an_obligation_another_attempt_proved(
    fx: Fixture, machine: Callable[[MachineBody], None]
) -> None:
    """Concurrent attempts on one obligation are normal. A late release from the loser must not
    drag a proved obligation back to `open` -- and this is a silent no-op, not an error, because
    nothing went wrong."""
    obligation = fx.obligation(status=ObligationStatus.PROVED)

    async def body(sm: ObligationStateMachine) -> None:
        await sm.retryable_failure(obligation)

    machine(body)
    assert fx.status_of(obligation) == "proved"


def test_budget_exhausted_is_refused_while_budget_remains(
    fx: Fixture, machine: Callable[[MachineBody], None]
) -> None:
    """The exhaustion test is made from the committed row, not taken on the caller's word -- a
    caller able to fail an obligation at will could retire work that still has budget."""
    obligation = fx.obligation(budget_attempts=8, spent_attempts=3)

    async def body(sm: ObligationStateMachine) -> None:
        with pytest.raises(Refused):
            await sm.budget_exhausted(obligation)

    machine(body)
    assert fx.status_of(obligation) == "in_progress"


def test_budget_exhausted_succeeds_when_spent(
    fx: Fixture, machine: Callable[[MachineBody], None]
) -> None:
    obligation = fx.obligation(budget_attempts=3, spent_attempts=3)

    async def body(sm: ObligationStateMachine) -> None:
        await sm.budget_exhausted(obligation)

    machine(body)
    assert fx.status_of(obligation) == "failed"


def test_blocked_requires_every_group_to_be_dead(
    fx: Fixture, machine: Callable[[MachineBody], None]
) -> None:
    """Competing groups exist precisely so one group's dead end does not kill the parent. A parent
    with a live group must stay `decomposed` however bad the other group looks."""
    parent = fx.obligation(status=ObligationStatus.DECOMPOSED)
    dead_group, live_group = uuid.uuid4(), uuid.uuid4()
    fx.edge(parent, fx.obligation(status=ObligationStatus.FAILED, depth=1), dead_group)
    live_child = fx.obligation(status=ObligationStatus.OPEN, depth=1)
    fx.edge(parent, live_child, live_group)

    async def body(sm: ObligationStateMachine) -> None:
        with pytest.raises(Refused):
            await sm.groups_exhausted(parent)

    machine(body)
    assert fx.status_of(parent) == "decomposed"


def test_blocked_succeeds_when_no_group_can_succeed(
    fx: Fixture, machine: Callable[[MachineBody], None]
) -> None:
    parent = fx.obligation(status=ObligationStatus.DECOMPOSED)
    for _ in range(2):
        group = uuid.uuid4()
        fx.edge(parent, fx.obligation(status=ObligationStatus.FAILED, depth=1), group)
        fx.edge(parent, fx.obligation(status=ObligationStatus.PROVED, depth=1), group)

    async def body(sm: ObligationStateMachine) -> None:
        await sm.groups_exhausted(parent)

    machine(body)
    assert fx.status_of(parent) == "blocked"


def test_blocked_is_refused_for_a_parent_with_no_groups(
    fx: Fixture, machine: Callable[[MachineBody], None]
) -> None:
    """ "No groups" is not "all groups dead". Treating it as such would block every obligation the
    moment anything looked at it."""
    parent = fx.obligation(status=ObligationStatus.DECOMPOSED)

    async def body(sm: ObligationStateMachine) -> None:
        with pytest.raises(Refused):
            await sm.groups_exhausted(parent)

    machine(body)
    assert fx.status_of(parent) == "decomposed"


def test_python_transition_table_agrees_with_sql(
    fx: Fixture, machine: Callable[[MachineBody], None]
) -> None:
    """The mirror-agreement check that makes `TRANSITIONS` safe to consult.

    The property asserted is precisely the one that matters: **no (status, outcome) pair the
    Python table calls illegal may change the obligation's status.** Some are refused loudly
    (`mark_failed` with budget remaining), others are accepted as deliberate no-ops
    (`mark_decomposed` on an already-decomposed parent, since competing groups are normal) --
    both are fine, and demanding an exception from all of them was the first version of this test
    and was simply wrong about the design. What must never happen is a silent transition the
    diagram has no arrow for.

    `PROVED` is excluded: `mark_proved`'s precondition is the §1.1 acceptance predicate over a
    *verdict*, not a status, and `tests/db/test_privileges.py` already exercises it in both
    directions (gate 8). This test is about the status half of each precondition.
    """
    outcomes = [o for o in ObligationOutcome if o != ObligationOutcome.PROVED]
    group = uuid.uuid4()

    async def attempt(
        sm: ObligationStateMachine, outcome: ObligationOutcome, oid: uuid.UUID
    ) -> None:
        match outcome:
            case ObligationOutcome.DECOMPOSED:
                await sm.mark_decomposed(oid, group)
            case ObligationOutcome.RETRYABLE_FAILURE:
                await sm.retryable_failure(oid)
            case ObligationOutcome.INFRA_ERROR:
                await sm.infra_error(oid)
            case ObligationOutcome.BUDGET_EXHAUSTED:
                await sm.budget_exhausted(oid)
            case ObligationOutcome.GROUPS_EXHAUSTED:
                await sm.groups_exhausted(oid)
            case _:  # pragma: no cover - PROVED is excluded above
                raise AssertionError(outcome)

    async def body(sm: ObligationStateMachine) -> None:
        for outcome in outcomes:
            for status in ObligationStatus:
                if is_legal(status, outcome):
                    continue
                # Everything *except* the status is set up to satisfy the transition -- exhausted
                # budget, an existing dead group -- so the status is unambiguously the objection.
                oid = fx.obligation(status=status, budget_attempts=1, spent_attempts=1)
                fx.edge(oid, fx.obligation(status=ObligationStatus.FAILED, depth=1), group)
                try:
                    await attempt(sm, outcome, oid)
                except Refused as exc:
                    assert "permission denied" not in str(exc), (
                        f"{outcome} from {status} was refused by the grant, not by its "
                        "precondition -- app must be able to *call* every transition function"
                    )
                assert fx.status_of(oid) == status.value, (
                    f"{outcome} from {status} changed the status, but TRANSITIONS has no such arrow"
                )

    machine(body)


def test_legal_transitions_are_all_reachable(
    fx: Fixture, machine: Callable[[MachineBody], None]
) -> None:
    """The other half of the mirror check: every pair the table calls *legal* must actually be
    accepted, and land where `resulting_status` says. Without this, `TRANSITIONS` could be trimmed
    to nothing and the illegal-pair test above would still pass.

    `PROVED` is again excluded (verdict-shaped precondition, covered by gate 8).
    """

    async def body(sm: ObligationStateMachine) -> None:
        for outcome in (o for o in ObligationOutcome if o != ObligationOutcome.PROVED):
            allowed_from, expected = TRANSITIONS[outcome]
            for status in allowed_from:
                oid = fx.obligation(status=status, budget_attempts=1, spent_attempts=1)
                group = uuid.uuid4()
                fx.edge(oid, fx.obligation(status=ObligationStatus.FAILED, depth=1), group)
                if outcome is ObligationOutcome.DECOMPOSED:
                    await sm.mark_decomposed(oid, group)
                elif outcome is ObligationOutcome.RETRYABLE_FAILURE:
                    await sm.retryable_failure(oid)
                elif outcome is ObligationOutcome.INFRA_ERROR:
                    await sm.infra_error(oid)
                elif outcome is ObligationOutcome.BUDGET_EXHAUSTED:
                    await sm.budget_exhausted(oid)
                else:
                    await sm.groups_exhausted(oid)
                assert fx.status_of(oid) == expected.value, (
                    f"{outcome} from {status} should reach {expected}"
                )

    machine(body)


def test_every_outcome_has_a_transition_entry() -> None:
    """A new outcome without a table entry would raise `KeyError` deep inside `is_legal`, at the
    call site rather than at definition."""
    assert set(TRANSITIONS) == set(ObligationOutcome)


def test_group_progress_summarizes_each_group_separately(
    fx: Fixture, app_async_database_url: str
) -> None:
    parent = fx.obligation(status=ObligationStatus.DECOMPOSED)
    complete, partial = uuid.uuid4(), uuid.uuid4()
    fx.edge(parent, fx.obligation(status=ObligationStatus.PROVED, depth=1), complete)
    fx.edge(parent, fx.obligation(status=ObligationStatus.PROVED, depth=1), complete)
    fx.edge(parent, fx.obligation(status=ObligationStatus.PROVED, depth=1), partial)
    fx.edge(parent, fx.obligation(status=ObligationStatus.FAILED, depth=1), partial)

    async def run() -> list[GroupProgress]:
        engine = create_async_engine(app_async_database_url)
        try:
            return await group_progress(async_sessionmaker(engine, expire_on_commit=False), parent)
        finally:
            await engine.dispose()

    by_id = {g.group_id: g for g in asyncio.run(run())}
    assert by_id[complete].all_proved is True
    assert by_id[complete].is_dead is False
    assert by_id[partial].all_proved is False
    assert by_id[partial].is_dead is True
    # One group fully proved is not "every group dead" -- the parent is reassemblable, not blocked.
    assert every_group_is_dead(list(by_id.values())) is False


def test_every_group_is_dead_is_false_with_no_groups() -> None:
    assert every_group_is_dead([]) is False


# --- Cycle guard (spec §5.3) ------------------------------------------------------------------
#
# "A cycle guard runs in the same transaction that inserts an edge -- a child's `goal_digest` may
# not equal any ancestor's, and `depth` is capped by `run.max_depth`. Without it, a policy can
# decompose an obligation into itself and consume budget forever."
#
# Driven as `app`, which holds INSERT on `obligation_edge` directly -- so a check the application
# performed instead would be advisory, and only a trigger is actually enforcement.


def _insert_edge_as_app(
    app_database_url: str, parent: uuid.UUID, child: uuid.UUID, group: uuid.UUID
) -> None:
    engine = create_engine(app_database_url)
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO obligation_edge (parent_id, child_id, group_id, role) "
                    "VALUES (:p, :c, :g, 'subgoal')"
                ),
                {"p": parent, "c": child, "g": group},
            )
            conn.commit()
    finally:
        engine.dispose()


def test_cycle_guard_rejects_a_self_edge(fx: Fixture, app_database_url: str) -> None:
    obligation = fx.obligation()
    with pytest.raises(DBAPIError, match="cannot be its own child"):
        _insert_edge_as_app(app_database_url, obligation, obligation, uuid.uuid4())


def test_cycle_guard_rejects_a_child_repeating_its_parents_goal(
    fx: Fixture, app_database_url: str
) -> None:
    """The simplest budget-burning cycle: decompose an obligation into itself under a new id."""
    digest = f"shared-{uuid.uuid4()}".encode()
    parent = fx.obligation(goal_digest=digest)
    child = fx.obligation(goal_digest=digest, depth=1)
    with pytest.raises(DBAPIError, match="decomposition cycle"):
        _insert_edge_as_app(app_database_url, parent, child, uuid.uuid4())


def test_cycle_guard_walks_the_whole_ancestor_chain(fx: Fixture, app_database_url: str) -> None:
    """Not just the immediate parent: a grandchild reintroducing its grandparent's goal is the
    same cycle one level further out, and catching only the direct parent would miss it."""
    digest = f"shared-{uuid.uuid4()}".encode()
    grandparent = fx.obligation(goal_digest=digest)
    parent = fx.obligation(depth=1)
    grandchild = fx.obligation(goal_digest=digest, depth=2)
    _insert_edge_as_app(app_database_url, grandparent, parent, uuid.uuid4())
    with pytest.raises(DBAPIError, match="decomposition cycle"):
        _insert_edge_as_app(app_database_url, parent, grandchild, uuid.uuid4())


def test_cycle_guard_enforces_run_max_depth(
    fx: Fixture, admin_engine: Engine, app_database_url: str
) -> None:
    """`run.max_depth` is the unconditional backstop for a cycle spelled two different ways, which
    `goal_digest` equality cannot catch (see `lean_agent_core.digests`). Its default is 6."""
    parent = fx.obligation()
    too_deep = fx.obligation(depth=7)
    with admin_engine.connect() as conn:
        max_depth = conn.execute(
            text("SELECT max_depth FROM run WHERE id = :id"), {"id": fx.run_id}
        ).scalar_one()
    assert max_depth == 6
    with pytest.raises(DBAPIError, match="past run max_depth"):
        _insert_edge_as_app(app_database_url, parent, too_deep, uuid.uuid4())


def test_cycle_guard_allows_an_ordinary_decomposition(fx: Fixture, app_database_url: str) -> None:
    """The guard must not be so eager that normal work is impossible -- two distinct subgoals of
    one parent, at depth 1, are exactly what every decomposition produces."""
    parent = fx.obligation()
    group = uuid.uuid4()
    for _ in range(2):
        _insert_edge_as_app(app_database_url, parent, fx.obligation(depth=1), group)


# --- The §1.1 acceptance predicate ------------------------------------------------------------


def _make_attempt_and_verdict(
    admin_engine: Engine,
    fx: Fixture,
    obligation_id: uuid.UUID,
    *,
    kind: str = "proved",
    link_ok: bool = True,
    replay_ok: bool = True,
    axiom_audit_ok: bool = True,
    observed: bytes | None = None,
) -> uuid.UUID:
    attempt_id = uuid.uuid4()
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO attempt (id, obligation_id, run_id, policy_id, policy_config_hash) "
                "VALUES (:id, :obl, :run, 'p', 'h')"
            ),
            {"id": attempt_id, "obl": obligation_id, "run": fx.run_id},
        )
        conn.execute(
            text(
                "INSERT INTO verdict (attempt_id, obligation_id, kind, link_ok, replay_ok, "
                "axiom_audit_ok, sealed_olean_sha_observed, elapsed_ms, toolchain_rev, "
                "mathlib_rev) VALUES (:id, :obl, CAST(:kind AS verdict_kind), :link, :replay, "
                ":audit, :observed, 1, 'v4.33.1', 'deadbeef')"
            ),
            {
                "id": attempt_id,
                "obl": obligation_id,
                "kind": kind,
                "link": link_ok,
                "replay": replay_ok,
                "audit": axiom_audit_ok,
                "observed": observed,
            },
        )
        conn.commit()
    return attempt_id


def _predicate_says(admin_engine: Engine, obligation_id: uuid.UUID, attempt_id: uuid.UUID) -> bool:
    with Session(admin_engine) as session:
        obligation = session.get_one(Obligation, obligation_id)
        verdict = session.get_one(Verdict, attempt_id)
        return acceptance_predicate(verdict, obligation)


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("kind is not proved", {"kind": "errors"}),
        ("did not link", {"link_ok": False}),
        ("did not replay", {"replay_ok": False}),
        ("failed the axiom audit", {"axiom_audit_ok": False}),
        ("observed no sealed digest", {"observed": None}),
        ("observed a different sealed digest", {"observed": b"some-other-bundle"}),
    ],
)
def test_acceptance_predicate_agrees_with_mark_proved_on_each_conjunct(
    fx: Fixture,
    admin_engine: Engine,
    machine: Callable[[MachineBody], None],
    label: str,
    overrides: dict[str, object],
) -> None:
    """Spec §1.1 is a conjunction of four things -- links, replays, audits, seal integrity -- and
    this drops each one in turn, asserting the Python copy and the SQL that actually holds the
    privilege reach the same verdict every time.

    The conjunct-at-a-time shape is deliberate: a predicate that accidentally ignored one clause
    would still pass a test that only ever showed it a fully-good and a fully-bad verdict.
    """
    sealed = f"sealed-{uuid.uuid4()}".encode()
    obligation = fx.obligation(sealed_olean_sha=sealed)
    kwargs: dict[str, object] = {"observed": sealed, **overrides}
    attempt = _make_attempt_and_verdict(admin_engine, fx, obligation, **kwargs)  # type: ignore[arg-type]

    assert _predicate_says(admin_engine, obligation, attempt) is False, label

    async def body(sm: ObligationStateMachine) -> None:
        with pytest.raises(Refused, match="acceptance predicate not satisfied"):
            await sm.mark_proved(obligation, attempt)

    machine(body)
    assert fx.status_of(obligation) == "in_progress"


def test_acceptance_predicate_and_mark_proved_both_accept_a_good_verdict(
    fx: Fixture, admin_engine: Engine, machine: Callable[[MachineBody], None]
) -> None:
    sealed = f"sealed-{uuid.uuid4()}".encode()
    obligation = fx.obligation(sealed_olean_sha=sealed)
    attempt = _make_attempt_and_verdict(admin_engine, fx, obligation, observed=sealed)

    assert _predicate_says(admin_engine, obligation, attempt) is True

    async def body(sm: ObligationStateMachine) -> None:
        await sm.mark_proved(obligation, attempt)
        # A second attempt succeeding on an already-proved obligation is normal, not an error.
        await sm.mark_proved(obligation, attempt)

    machine(body)
    assert fx.status_of(obligation) == "proved"


def test_a_decomposed_parent_is_proved_through_mark_proved_like_anything_else(
    fx: Fixture,
    admin_engine: Engine,
    app_async_database_url: str,
    machine: Callable[[MachineBody], None],
) -> None:
    """Spec §6.4: "There is no 'all children proved implies parent proved' shortcut. Reassembly
    produces a real attempt with a real link and flows through `mark_proved` like anything else."

    So a `decomposed` parent whose children are all proved is *not* proved by that fact -- it
    becomes proved only by presenting its own verdict, which is what this asserts.
    """
    sealed = f"sealed-{uuid.uuid4()}".encode()
    parent = fx.obligation(status=ObligationStatus.DECOMPOSED, sealed_olean_sha=sealed)
    group = uuid.uuid4()
    for _ in range(2):
        fx.edge(parent, fx.obligation(status=ObligationStatus.PROVED, depth=1), group)

    (progress,) = asyncio.run(_read_group_progress(app_async_database_url, parent))
    assert progress.all_proved is True
    assert fx.status_of(parent) == "decomposed"  # children alone changed nothing

    attempt = _make_attempt_and_verdict(admin_engine, fx, parent, observed=sealed)

    async def body(sm: ObligationStateMachine) -> None:
        await sm.mark_proved(parent, attempt)

    machine(body)
    assert fx.status_of(parent) == "proved"


async def _read_group_progress(url: str, obligation_id: uuid.UUID) -> list[GroupProgress]:
    """As `app`, which holds SELECT on everything -- `str(admin_engine.url)` would be the obvious
    shortcut and does not work: SQLAlchemy masks the password as `***` when rendering a URL."""
    engine = create_async_engine(url)
    try:
        return await group_progress(
            async_sessionmaker(engine, expire_on_commit=False), obligation_id
        )
    finally:
        await engine.dispose()
