"""Privilege model tests (spec §5.5, Phase 1 gate 8): tested against a live PostgreSQL, never a
mock -- the whole point of `deploy/grants.sql` is that Postgres itself refuses things, which a
mock cannot exercise. Covers all four named checks: status bypass refused, permitted-column
update allowed, worker (`app`) INSERT INTO verdict refused, repeated `mark_proved` idempotent.

Local dev / CI setup is the same as `test_schema.py` (Postgres 16, migrations applied), plus
`deploy/grants.sql` itself -- note the plain `postgresql://` URL, not `DATABASE_URL`'s
`postgresql+asyncpg://` (psql/libpq don't understand the SQLAlchemy driver suffix):
    psql postgresql://postgres:postgres@localhost:5432/leanagent -f deploy/grants.sql
CI applies it via psycopg instead (see .github/workflows/ci.yml), so it doesn't depend on a
Postgres client being preinstalled on the runner.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import NamedTuple

import pytest
from lean_agent_core.enums import VerdictKind
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError

ADMIN_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/leanagent",
)
# Test-only credentials for the two application roles `grants.sql` creates. Never used outside
# this suite; production credential management is out of scope here (see grants.sql itself,
# which deliberately creates the roles with no password at all).
_APP_PASSWORD = "app_test_password"
_LEANSERV_PASSWORD = "leanserv_test_password"


def _role_url(role: str, password: str) -> str:
    base = ADMIN_DATABASE_URL.rsplit("@", 1)[1]  # "host:port/db"
    driver = ADMIN_DATABASE_URL.split("://", 1)[0]
    return f"{driver}://{role}:{password}@{base}"


APP_DATABASE_URL = _role_url("app", _APP_PASSWORD)
LEANSERV_DATABASE_URL = _role_url("leanserv", _LEANSERV_PASSWORD)


@pytest.fixture(scope="session")
def admin_engine() -> Iterator[Engine]:
    eng = create_engine(ADMIN_DATABASE_URL)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as e:
        pytest.skip(f"Postgres not reachable at {ADMIN_DATABASE_URL!r} ({e.__class__.__name__})")

    # Give the two roles a password so this suite can connect as them over TCP; grants.sql
    # itself deliberately leaves them password-less (real deployments manage credentials
    # separately, e.g. via a secrets manager, not a checked-in SQL file).
    with eng.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'app'")).first()
        if exists is None:
            pytest.skip(
                "Roles 'app'/'leanserv' don't exist -- run `psql \"$DATABASE_URL\" -f "
                "deploy/grants.sql` against this database first."
            )
        conn.execute(text(f"ALTER ROLE app PASSWORD '{_APP_PASSWORD}'"))
        conn.execute(text(f"ALTER ROLE leanserv PASSWORD '{_LEANSERV_PASSWORD}'"))
        conn.commit()

    yield eng
    eng.dispose()


def _digest(label: str) -> bytes:
    return f"digest-{label}-{uuid.uuid4()}".encode()


class _SealedObligation(NamedTuple):
    id: uuid.UUID
    run_id: uuid.UUID
    sealed_olean_sha: bytes


@pytest.fixture
def sealed_obligation(admin_engine: Engine) -> Iterator[_SealedObligation]:
    """A genuinely committed obligation + run + base_env, cleaned up explicitly afterward.

    Unlike `test_schema.py`'s fixtures, this data must be visible to entirely separate
    connections opened as `app`/`leanserv` -- other roles' connections can never see another
    transaction's *uncommitted* work (ordinary MVCC visibility), so the "wrap the test in a
    transaction and roll it back" pattern used there cannot be reused here. Confirmed empirically:
    the first version of this fixture used exactly that pattern and every role-scoped test failed
    with a foreign-key violation, because the row it referenced was never actually committed.
    """
    base_env_digest = _digest("base")
    run_id = uuid.uuid4()
    obligation_id = uuid.uuid4()
    sealed_olean_sha = _digest("sealed")

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
                "INSERT INTO run (id, tenant_id, base_env_digest, status, manifest, "
                "manifest_hash) VALUES (:id, :tenant, :base_env, 'running', '{}', :mh)"
            ),
            {
                "id": run_id,
                "tenant": uuid.uuid4(),
                "base_env": base_env_digest,
                "mh": _digest("manifest"),
            },
        )
        conn.execute(
            text(
                "INSERT INTO obligation (id, run_id, base_env_digest, goal_digest, "
                "sealed_olean_sha, goal_src, decl_name) "
                "VALUES (:id, :run_id, :base_env, :goal_digest, :sealed, 'theorem foo : True "
                ":= trivial', 'foo')"
            ),
            {
                "id": obligation_id,
                "run_id": run_id,
                "base_env": base_env_digest,
                "goal_digest": _digest("goal"),
                "sealed": sealed_olean_sha,
            },
        )
        conn.commit()

    yield _SealedObligation(id=obligation_id, run_id=run_id, sealed_olean_sha=sealed_olean_sha)

    with admin_engine.connect() as conn:
        # Cascades to obligation, attempt, verdict, obligation_edge (all ON DELETE CASCADE from
        # run/obligation); base_env has no such cascade from either, so it needs its own delete.
        conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": run_id})
        conn.execute(
            text("DELETE FROM base_env WHERE digest = :digest"), {"digest": base_env_digest}
        )
        conn.commit()


def test_app_cannot_update_status_directly(
    admin_engine: Engine, sealed_obligation: _SealedObligation
) -> None:
    """Gate 8: status bypass refused. `app` has no table-level UPDATE on `obligation` and
    `status` is not in its permitted-column grant -- so this must fail at the database, not
    merely be something the application layer chooses not to do."""
    app_engine = create_engine(APP_DATABASE_URL)
    try:
        with app_engine.connect() as conn, pytest.raises(DBAPIError, match="permission denied"):
            conn.execute(
                text("UPDATE obligation SET status = 'proved' WHERE id = :id"),
                {"id": str(sealed_obligation.id)},
            )
    finally:
        app_engine.dispose()


def test_app_can_update_permitted_columns(
    admin_engine: Engine, sealed_obligation: _SealedObligation
) -> None:
    """Gate 8: permitted-column update allowed. `priority` is explicitly granted."""
    app_engine = create_engine(APP_DATABASE_URL)
    try:
        with app_engine.connect() as conn:
            conn.execute(
                text("UPDATE obligation SET priority = 5.0 WHERE id = :id"),
                {"id": str(sealed_obligation.id)},
            )
            conn.commit()
    finally:
        app_engine.dispose()

    with admin_engine.connect() as conn:
        priority = conn.execute(
            text("SELECT priority FROM obligation WHERE id = :id"),
            {"id": str(sealed_obligation.id)},
        ).scalar_one()
    assert priority == 5.0


def test_app_cannot_insert_verdict(
    admin_engine: Engine, sealed_obligation: _SealedObligation
) -> None:
    """Gate 8: worker (app) INSERT INTO verdict refused -- leanserv is the only writer of
    verdicts (spec §5.1, §6.2). Uses a real attempt row (inserted as `app`, which *is* permitted
    to insert attempts) so the verdict insert fails on its own merits, not because the attempt
    it references doesn't exist."""
    with create_engine(APP_DATABASE_URL).connect() as conn:
        attempt_id = conn.execute(
            text(
                "INSERT INTO attempt (id, obligation_id, run_id, policy_id, policy_config_hash) "
                "VALUES (gen_random_uuid(), :obl, :run, 'p', 'h') RETURNING id"
            ),
            {"obl": str(sealed_obligation.id), "run": str(sealed_obligation.run_id)},
        ).scalar_one()
        conn.commit()

        with pytest.raises(DBAPIError, match="permission denied"):
            conn.execute(
                text(
                    "INSERT INTO verdict (attempt_id, obligation_id, kind, link_ok, replay_ok, "
                    "axiom_audit_ok, elapsed_ms, toolchain_rev, mathlib_rev) "
                    "VALUES (:att, :obl, 'proved', true, true, true, 1, 'v', 'v')"
                ),
                {"att": str(attempt_id), "obl": str(sealed_obligation.id)},
            )


def test_leanserv_can_insert_verdict(
    admin_engine: Engine, sealed_obligation: _SealedObligation
) -> None:
    """Complement to the above: leanserv is explicitly granted INSERT on verdict, and must
    actually be able to use it, not just have app correctly denied."""
    with create_engine(APP_DATABASE_URL).connect() as conn:
        attempt_id = conn.execute(
            text(
                "INSERT INTO attempt (id, obligation_id, run_id, policy_id, policy_config_hash) "
                "VALUES (gen_random_uuid(), :obl, :run, 'p', 'h') RETURNING id"
            ),
            {"obl": str(sealed_obligation.id), "run": str(sealed_obligation.run_id)},
        ).scalar_one()
        conn.commit()

    leanserv_engine = create_engine(LEANSERV_DATABASE_URL)
    try:
        with leanserv_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO verdict (attempt_id, obligation_id, kind, link_ok, replay_ok, "
                    "axiom_audit_ok, elapsed_ms, toolchain_rev, mathlib_rev) "
                    "VALUES (:att, :obl, 'proved', true, true, true, 1, 'v', 'v')"
                ),
                {"att": str(attempt_id), "obl": str(sealed_obligation.id)},
            )
            conn.commit()
    finally:
        leanserv_engine.dispose()

    with admin_engine.connect() as conn:
        kind = conn.execute(
            text("SELECT kind FROM verdict WHERE attempt_id = :att"), {"att": str(attempt_id)}
        ).scalar_one()
    assert kind == VerdictKind.PROVED.value


def _insert_attempt_and_verdict(
    obligation_id: uuid.UUID, run_id: uuid.UUID, *, satisfies_predicate: bool
) -> uuid.UUID:
    """Insert an attempt (as `app`) and its verdict (as `leanserv`), returning the attempt id.
    `satisfies_predicate` controls whether the verdict actually meets `mark_proved`'s acceptance
    predicate (all three ok flags true, kind='proved', sealed_olean_sha_observed matches)."""
    with create_engine(APP_DATABASE_URL).connect() as app_conn:
        attempt_id: uuid.UUID = app_conn.execute(
            text(
                "INSERT INTO attempt (id, obligation_id, run_id, policy_id, policy_config_hash) "
                "VALUES (gen_random_uuid(), :obl, :run, 'p', 'h') RETURNING id"
            ),
            {"obl": str(obligation_id), "run": str(run_id)},
        ).scalar_one()
        app_conn.commit()

    with create_engine(LEANSERV_DATABASE_URL).connect() as leanserv_conn:
        if satisfies_predicate:
            leanserv_conn.execute(
                text(
                    "INSERT INTO verdict (attempt_id, obligation_id, kind, link_ok, replay_ok, "
                    "axiom_audit_ok, sealed_olean_sha_observed, elapsed_ms, toolchain_rev, "
                    "mathlib_rev) "
                    "SELECT :att, :obl, 'proved', true, true, true, sealed_olean_sha, 1, 'v', 'v' "
                    "FROM obligation WHERE id = :obl"
                ),
                {"att": str(attempt_id), "obl": str(obligation_id)},
            )
        else:
            leanserv_conn.execute(
                text(
                    "INSERT INTO verdict (attempt_id, obligation_id, kind, link_ok, replay_ok, "
                    "axiom_audit_ok, elapsed_ms, toolchain_rev, mathlib_rev) "
                    "VALUES (:att, :obl, 'errors', false, true, true, 1, 'v', 'v')"
                ),
                {"att": str(attempt_id), "obl": str(obligation_id)},
            )
        leanserv_conn.commit()
    return attempt_id


def test_mark_proved_rejects_unsatisfied_predicate(
    admin_engine: Engine, sealed_obligation: _SealedObligation
) -> None:
    attempt_id = _insert_attempt_and_verdict(
        sealed_obligation.id, sealed_obligation.run_id, satisfies_predicate=False
    )
    app_engine = create_engine(APP_DATABASE_URL)
    try:
        with (
            app_engine.connect() as conn,
            pytest.raises(DBAPIError, match="acceptance predicate not satisfied"),
        ):
            conn.execute(
                text("SELECT mark_proved(:obl, :att)"),
                {"obl": str(sealed_obligation.id), "att": str(attempt_id)},
            )
    finally:
        app_engine.dispose()


def test_mark_proved_succeeds_and_is_idempotent(
    admin_engine: Engine, sealed_obligation: _SealedObligation
) -> None:
    """Gate 8: repeated mark_proved idempotent. Also exercises that `app` -- which cannot UPDATE
    obligation.status directly (see test_app_cannot_update_status_directly) -- can still reach
    'proved' through this one sanctioned path, because SECURITY DEFINER runs it with the
    function owner's privileges, not the caller's."""
    attempt_id = _insert_attempt_and_verdict(
        sealed_obligation.id, sealed_obligation.run_id, satisfies_predicate=True
    )
    app_engine = create_engine(APP_DATABASE_URL)
    try:
        with app_engine.connect() as conn:
            conn.execute(
                text("SELECT mark_proved(:obl, :att)"),
                {"obl": str(sealed_obligation.id), "att": str(attempt_id)},
            )
            conn.commit()

        with admin_engine.connect() as check_conn:
            status = check_conn.execute(
                text("SELECT status FROM obligation WHERE id = :id"),
                {"id": str(sealed_obligation.id)},
            ).scalar_one()
        assert status == "proved"

        # Calling it again with the same (now-non-'open'/'in_progress'/'decomposed') obligation
        # must be a silent no-op, not an error -- concurrent attempts succeeding is normal.
        with app_engine.connect() as conn:
            conn.execute(
                text("SELECT mark_proved(:obl, :att)"),
                {"obl": str(sealed_obligation.id), "att": str(attempt_id)},
            )
            conn.commit()

        with admin_engine.connect() as check_conn:
            status_after = check_conn.execute(
                text("SELECT status FROM obligation WHERE id = :id"),
                {"id": str(sealed_obligation.id)},
            ).scalar_one()
        assert status_after == "proved"
    finally:
        app_engine.dispose()
