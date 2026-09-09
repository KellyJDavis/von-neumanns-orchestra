"""Schema round-trip tests (spec §5.4 exit criterion): every table can be written and read back
through the ORM, and the result validates against its Pydantic mirror unchanged.

Runs against a real PostgreSQL instance with migrations already applied -- never a mock (see
CLAUDE.md). Local dev: `docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16`,
then `alembic upgrade head`, then `TEST_DATABASE_URL=... pytest tests/db`. CI wires the same two
steps against its own Postgres service before running the suite.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from lean_agent_core.enums import ObligationStatus, ProvenanceClass, TrustClass, VerdictKind
from lean_agent_core.orm import (
    Attempt,
    BaseEnv,
    Obligation,
    ToolCall,
    Trajectory,
    Verdict,
    VerificationCache,
)
from lean_agent_core.schemas import (
    BaseEnvSchema,
    ObligationSchema,
    ToolCallSchema,
    TrajectorySchema,
    VerdictSchema,
    VerificationCacheSchema,
)
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/leanagent",
)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(TEST_DATABASE_URL)
    # A connection-level failure (Postgres not running locally) skips gracefully -- honest about
    # not having run, not a fake pass -- rather than every test failing with the same opaque
    # connection error. CI always provides a real Postgres service (see .github/workflows/ci.yml)
    # specifically so this skip never triggers there; per CLAUDE.md, tests/db/ must run against a
    # real instance, never a mock, and skipping is not a substitute for that in CI.
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as e:
        pytest.skip(
            f"Postgres not reachable at {TEST_DATABASE_URL!r} ({e.__class__.__name__}); "
            "start one and run `alembic upgrade head`, or set TEST_DATABASE_URL."
        )
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Each test runs in its own transaction, rolled back afterward -- tests never depend on
    (or leave behind) each other's rows.

    A test that triggers a database error (e.g. a NOT NULL violation) and doesn't itself roll
    back leaves the connection's transaction aborted; the `Session` deassociates from it at that
    point rather than leaving it for this fixture to close. `transaction.is_active` distinguishes
    that case from the normal one, so cleanup doesn't call `rollback()` twice.
    """
    connection = engine.connect()
    transaction = connection.begin()
    with Session(bind=connection) as db:
        yield db
    if transaction.is_active:
        transaction.rollback()
    connection.close()


def _digest(label: str) -> bytes:
    return f"digest-{label}-{uuid.uuid4()}".encode()


@pytest.fixture
def base_env(session: Session) -> BaseEnv:
    row = BaseEnv(
        digest=_digest("base"),
        recipe={"imports": ["Mathlib"]},
        toolchain_rev="v4.33.1",
        mathlib_rev="deadbeef",
    )
    session.add(row)
    session.flush()
    return row


def test_base_env_round_trips(session: Session, base_env: BaseEnv) -> None:
    fetched = session.get(BaseEnv, base_env.digest)
    assert fetched is not None
    schema = BaseEnvSchema.model_validate(fetched)
    assert schema.digest == base_env.digest
    assert schema.recipe == {"imports": ["Mathlib"]}
    assert schema.curated is False  # server default, applied on flush
    assert schema.created_at is not None


@pytest.fixture
def obligation(session: Session, base_env: BaseEnv) -> Obligation:
    from lean_agent_core.orm import Run

    run = Run(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        base_env_digest=base_env.digest,
        status="running",
        manifest={"schema_version": "1.0"},
        manifest_hash=_digest("manifest"),
    )
    session.add(run)
    session.flush()

    obl = Obligation(
        run_id=run.id,
        base_env_digest=base_env.digest,
        goal_digest=_digest("goal"),
        sealed_olean_sha=_digest("sealed"),
        goal_src="theorem foo : True := trivial",
        decl_name="foo",
    )
    session.add(obl)
    session.flush()
    return obl


def test_obligation_round_trips_with_enum_and_defaults(
    session: Session, obligation: Obligation
) -> None:
    fetched = session.get(Obligation, obligation.id)
    assert fetched is not None
    schema = ObligationSchema.model_validate(fetched)
    # Enum comes back as the Python enum, not a bare string -- proves _pg_enum's
    # values_callable round-trips through asyncpg/psycopg correctly, not just on the Python side.
    assert schema.status == ObligationStatus.OPEN
    assert isinstance(schema.status, ObligationStatus)
    assert schema.budget_attempts == 8  # spec default


def test_run_axiom_allowlist_default_round_trips(session: Session, base_env: BaseEnv) -> None:
    from lean_agent_core.orm import Run
    from lean_agent_core.schemas import RunSchema

    run = Run(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        base_env_digest=base_env.digest,
        status="running",
        manifest={},
        manifest_hash=_digest("manifest2"),
    )
    session.add(run)
    session.flush()

    fetched = session.get(Run, run.id)
    assert fetched is not None
    schema = RunSchema.model_validate(fetched)
    assert schema.axiom_allowlist == ["propext", "Classical.choice", "Quot.sound"]


@pytest.fixture
def attempt(session: Session, obligation: Obligation) -> Attempt:
    att = Attempt(
        obligation_id=obligation.id,
        run_id=obligation.run_id,
        policy_id="SymbolicPortfolio",
        policy_config_hash=_digest("policy"),
    )
    session.add(att)
    session.flush()
    return att


def test_verdict_round_trips(session: Session, attempt: Attempt, obligation: Obligation) -> None:
    verdict = Verdict(
        attempt_id=attempt.id,
        obligation_id=obligation.id,
        kind=VerdictKind.PROVED,
        link_ok=True,
        replay_ok=True,
        axiom_audit_ok=True,
        elapsed_ms=42,
        toolchain_rev="v4.33.1",
        mathlib_rev="deadbeef",
    )
    session.add(verdict)
    session.flush()

    fetched = session.get(Verdict, attempt.id)
    assert fetched is not None
    schema = VerdictSchema.model_validate(fetched)
    assert schema.kind == VerdictKind.PROVED
    assert schema.link_ok and schema.replay_ok and schema.axiom_audit_ok


def test_trajectory_without_provenance_is_rejected_by_the_database(
    session: Session, attempt: Attempt
) -> None:
    """Spec §7.1: `trajectory.provenance` is NOT NULL with no default. SQLAlchemy's generated
    `__init__` accepts every mapped column as an optional keyword argument regardless of
    nullability, so omitting `provenance` does not fail at construction time -- only the
    database's own NOT NULL constraint catches it, at flush. That is the actual enforcement
    boundary, so that is what this test asserts against, rather than a Python-level check that
    doesn't exist. A separate test (below) covers the successful case, rather than recovering
    from this one's expected failure and continuing in the same transaction.
    """
    from sqlalchemy.exc import IntegrityError

    session.add(
        Trajectory(  # type: ignore[call-arg]
            attempt_id=attempt.id,
            sampling={},
            steps_blob=b"[]",
            n_steps=0,
        )
    )
    with pytest.raises(IntegrityError, match="provenance"):
        session.flush()


def test_trajectory_round_trips_with_explicit_provenance(
    session: Session, attempt: Attempt
) -> None:
    traj = Trajectory(
        attempt_id=attempt.id,
        provenance=ProvenanceClass.SYMBOLIC,
        sampling={},
        steps_blob=b"[]",
        n_steps=0,
    )
    session.add(traj)
    session.flush()

    fetched = session.get(Trajectory, attempt.id)
    assert fetched is not None
    schema = TrajectorySchema.model_validate(fetched)
    assert schema.provenance == ProvenanceClass.SYMBOLIC


def test_tool_call_round_trips(session: Session, attempt: Attempt) -> None:
    call = ToolCall(
        attempt_id=attempt.id,
        step_index=0,
        server="search",
        tool="loogle",
        trust=TrustClass.RETRIEVAL,
        args_blob=b'{"query": "foo"}',
        ok=True,
        latency_ms=120,
    )
    session.add(call)
    session.flush()

    fetched = session.get(ToolCall, call.id)
    assert fetched is not None
    schema = ToolCallSchema.model_validate(fetched)
    assert schema.trust == TrustClass.RETRIEVAL
    assert schema.cost_usd == 0


def test_verification_cache_round_trips(session: Session) -> None:
    cache_key = _digest("cache")
    entry = VerificationCache(
        cache_key=cache_key,
        kind=VerdictKind.PROVED,
        elapsed_ms=10,
        toolchain_rev="v4.33.1",
        mathlib_rev="deadbeef",
    )
    session.add(entry)
    session.flush()

    fetched = session.get(VerificationCache, cache_key)
    assert fetched is not None
    schema = VerificationCacheSchema.model_validate(fetched)
    assert schema.kind == VerdictKind.PROVED
    assert schema.hits == 0
