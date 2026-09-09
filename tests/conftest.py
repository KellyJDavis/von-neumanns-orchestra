"""Shared Postgres fixtures, usable from any test directory -- extracted from test_privileges.py
(M1.6) into tests/db/conftest.py once a third test file (M1.8.4's test_cache.py/test_verdicts.py)
needed the same admin-engine-plus-role-password setup, then promoted here (top-level, an ancestor
of every tests/* directory) once M1.8.5's tests/leanserv/test_api.py needed the same fixtures
from *outside* tests/db/ -- a sibling directory's conftest.py isn't visible to pytest's fixture
lookup, only an ancestor's is.

Tests here run against a live PostgreSQL, never a mock (see CLAUDE.md) -- `admin_engine` skips
gracefully (not a fake pass) if Postgres isn't reachable, or if `deploy/grants.sql` hasn't been
applied yet, so a fresh local checkout fails honestly rather than opaquely. CI always provides
both (see .github/workflows/ci.yml) specifically so these skips never trigger there.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import NamedTuple

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError

ADMIN_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/leanagent",
)
# Test-only credentials for the two application roles `grants.sql` creates. Never used outside
# this suite; production credential management is out of scope here (see grants.sql itself,
# which deliberately creates the roles with no password at all).
_APP_PASSWORD = "app_test_password"
_LEANSERV_PASSWORD = "leanserv_test_password"


def _role_url(role: str, password: str, *, driver: str | None = None) -> str:
    base = ADMIN_DATABASE_URL.rsplit("@", 1)[1]  # "host:port/db"
    driver = driver or ADMIN_DATABASE_URL.split("://", 1)[0]
    return f"{driver}://{role}:{password}@{base}"


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


@pytest.fixture(scope="session")
def app_database_url(admin_engine: Engine) -> str:
    del admin_engine  # depended on only to order after its password-setup side effect
    return _role_url("app", _APP_PASSWORD)


@pytest.fixture(scope="session")
def leanserv_database_url(admin_engine: Engine) -> str:
    del admin_engine
    return _role_url("leanserv", _LEANSERV_PASSWORD)


@pytest.fixture(scope="session")
def leanserv_async_database_url(admin_engine: Engine) -> str:
    """Same role and credentials as `leanserv_database_url`, but with the `asyncpg` driver --
    for `lean_agent_serv.cache`/`.verdicts`, which are async (leanserv's own control loop and
    pool are asyncio throughout) unlike this test suite's own sync admin/setup connections.
    """
    del admin_engine
    return _role_url("leanserv", _LEANSERV_PASSWORD, driver="postgresql+asyncpg")


def _digest(label: str) -> bytes:
    return f"digest-{label}-{uuid.uuid4()}".encode()


class SealedObligation(NamedTuple):
    id: uuid.UUID
    run_id: uuid.UUID
    sealed_olean_sha: bytes


@pytest.fixture
def sealed_obligation(admin_engine: Engine) -> Iterator[SealedObligation]:
    """A genuinely committed obligation + run + base_env, cleaned up explicitly afterward.

    Originally test_privileges.py-only (M1.6); moved here once M1.8.4's test_verdicts.py needed
    the same fixture data (`VerdictWriter` needs a real `obligation`/`attempt` to reference).

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

    yield SealedObligation(id=obligation_id, run_id=run_id, sealed_olean_sha=sealed_olean_sha)

    with admin_engine.connect() as conn:
        # Cascades to obligation, attempt, verdict, obligation_edge (all ON DELETE CASCADE from
        # run/obligation); base_env has no such cascade from either, so it needs its own delete.
        conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": run_id})
        conn.execute(
            text("DELETE FROM base_env WHERE digest = :digest"), {"digest": base_env_digest}
        )
        conn.commit()
