"""Shared Postgres and Lean-toolchain fixtures, usable from any test directory -- extracted from
test_privileges.py
(M1.6) into tests/db/conftest.py once a third test file (M1.8.4's test_cache.py/test_verdicts.py)
needed the same admin-engine-plus-role-password setup, then promoted here (top-level, an ancestor
of every tests/* directory) once M1.8.5's tests/leanserv/test_api.py needed the same fixtures
from *outside* tests/db/ -- a sibling directory's conftest.py isn't visible to pytest's fixture
lookup, only an ancestor's is.

Tests here run against a live PostgreSQL and a real `leankernel` process, never a mock (see
CLAUDE.md) -- `admin_engine` and `lake_project_dir` skip gracefully (not a fake pass) if their
prerequisite isn't available, so a fresh local checkout fails honestly rather than opaquely. CI
always provides both (see .github/workflows/ci.yml) specifically so these skips never trigger
there -- and `LEANKERNEL_REQUIRED` makes that a checked claim rather than an assumed one; see
`lake_project_dir`.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError

LEANKERNEL_DIR = Path(__file__).resolve().parents[1] / "packages" / "leankernel"
LEANKERNEL_EXE = LEANKERNEL_DIR / ".lake" / "build" / "bin" / "leankernel"


@pytest.fixture(scope="session")
def lake_project_dir() -> Path:
    """The Lake project every test that spawns a real Lean worker runs against.

    Skips when the exe isn't built (Lean toolchain not set up, or `lake build` not yet run) rather
    than failing every such test with the same opaque "file not found" -- the same
    honesty-over-fake-pass convention `admin_engine`'s Postgres check uses.

    `LEANKERNEL_REQUIRED=1` turns that skip into a hard failure, and CI's `lean` job sets it.
    Without it the graceful skip is indistinguishable from a pass in aggregate output, which is
    not hypothetical: `lean_exe leankernel` was not a `@[default_target]`, so CI's `lake build`
    built the library and never linked the binary, and *every* test depending on this fixture
    silently skipped in CI from M1.8.2 until M2.1.1 found it (`tests/leanserv tests/eval` ran in
    2 seconds, which is what gave it away). The lakefile is fixed, and this is what stops the same
    class of gap from going unnoticed again.
    """
    if not LEANKERNEL_EXE.exists():
        message = (
            f"{LEANKERNEL_EXE} not built; run `lake build` in {LEANKERNEL_DIR} first. "
            "CI always builds it before this suite runs (see .github/workflows/ci.yml)."
        )
        if os.environ.get("LEANKERNEL_REQUIRED") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return LEANKERNEL_DIR


#: The two goals `materialized_bundle` seals: a `Prop` and a universe-polymorphic data goal, the
#: same pair `LeanKernelTests/Goals.lean` uses so both of Link's branches (`thmDecl`/`defnDecl`)
#: are exercised through the HTTP surface too.
BUNDLE_SOURCE = """import Init
set_option autoImplicit false
set_option relaxedAutoImplicit false
namespace LeanAgent.Goals
def G_add_zero : Sort _ := ∀ n : Nat, n + 0 = n
def G_poly : Sort _ := PUnit
end LeanAgent.Goals
"""


@dataclass(frozen=True)
class MaterializedBundle:
    root: Path
    sha: str
    olean_digest: bytes


@pytest.fixture(scope="session")
def materialized_bundle(
    lake_project_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> MaterializedBundle:
    """A real compiled sealed bundle, laid out the way spec §4.1 names it
    (`LeanAgent/Goals/Bundle_<digest>.lean`) under a root that becomes the pool's `bundle_root`.

    Compiled with `lake env lean --root=<root>`, not `lake build`: the bundle deliberately lives
    outside the Lake package (it is generated per run, not a checked-in target), and `lake env
    lean` refuses a file outside the package root unless `--root` says otherwise. Session-scoped
    because compiling it costs a real Lean invocation and nothing mutates it.
    """
    sha = hashlib.sha256(BUNDLE_SOURCE.encode()).hexdigest()
    root = tmp_path_factory.mktemp("bundle_root")
    module_dir = root / "LeanAgent" / "Goals"
    module_dir.mkdir(parents=True)
    source = module_dir / f"Bundle_{sha}.lean"
    source.write_text(BUNDLE_SOURCE)
    olean = module_dir / f"Bundle_{sha}.olean"
    subprocess.run(
        ["lake", "env", "lean", f"--root={root}", str(source), "-o", str(olean)],
        cwd=lake_project_dir,
        check=True,
        capture_output=True,
    )
    return MaterializedBundle(
        root=root, sha=sha, olean_digest=hashlib.sha256(olean.read_bytes()).digest()
    )


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
def app_async_database_url(admin_engine: Engine) -> str:
    """The `app` role over asyncpg. `state.py`'s transition functions must be exercised as the
    role that will actually call them in production -- running them as the admin/superuser would
    prove nothing about the privilege model, since the whole design rests on `app` being unable to
    write `obligation.status` by any other route.
    """
    del admin_engine
    return _role_url("app", _APP_PASSWORD, driver="postgresql+asyncpg")


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
