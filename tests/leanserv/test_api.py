"""M1.8.5/M2.1.1 exit criterion: the FastAPI surface (`/v1/check`, `/v1/check_batch`, `/v1/seal`,
`/v1/health`) wired to real infrastructure throughout -- a genuinely spawned `leankernel serve` process
(M1.8.1/M1.8.2) via a real `LeanReplPool` (M1.8.3), and a live Postgres connected as the real
`leanserv` role for the cache (M1.8.4) and the `base_env` lookup, never mocked.

This suite needs *both* the built `leankernel` exe and a live Postgres with `deploy/grants.sql`
applied -- unlike `test_repl.py`/`test_pool.py` (Lean only) or `tests/db/`'s suites (Postgres
only). It lives here, under `tests/leanserv/`, and CI's `lean` job grew a Postgres service
container specifically so both prerequisites are available in the one job that already pays for
the (expensive) Lean toolchain/Mathlib setup -- see CLAUDE.md and `.github/workflows/ci.yml`.

No pytest-asyncio: `TestClient` manages its own event loop internally, so these are plain sync
`def test_...` functions calling `client.post`/`client.get` directly, same as any other requests-
style HTTP test -- no `asyncio.run` needed here at all, unlike `test_repl.py`/`test_pool.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lean_agent_core.blobs import LocalBlobStore
from lean_agent_serv.api import create_app
from lean_agent_serv.cache import VerificationCacheStore
from lean_agent_serv.pool import LeanReplPool, PoolConfig
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
def registered_base_env(admin_engine: Engine) -> Iterator[str]:
    """A real, committed `base_env` row with a Mathlib-free (`Init`-only) recipe, for speed --
    returns its digest as a hex string, the same encoding `/v1/check` expects on the wire.
    """
    digest = f"api-test-{uuid.uuid4()}".encode()
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO base_env (digest, recipe, toolchain_rev, mathlib_rev) "
                "VALUES (:digest, '{\"imports\": [\"Init\"]}', 'v4.33.1', 'deadbeef')"
            ),
            {"digest": digest},
        )
        conn.commit()
    yield digest.hex()
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM base_env WHERE digest = :digest"), {"digest": digest})
        conn.commit()


@pytest.fixture(autouse=True)
def _cleanup_verification_cache(admin_engine: Engine) -> Iterator[None]:
    yield
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM verification_cache"))
        conn.commit()


@pytest.fixture
def client(
    lake_project_dir: Path, leanserv_async_database_url: str, tmp_path: Path
) -> Iterator[TestClient]:
    engine = create_async_engine(leanserv_async_database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    pool = LeanReplPool(lake_project_dir, PoolConfig(max_total_workers=4))
    cache = VerificationCacheStore(sessionmaker, LocalBlobStore(tmp_path))
    app = create_app(pool, cache, sessionmaker)

    with TestClient(app) as c:
        yield c

    # Not `asyncio.run(pool.aclose())` here: `create_app`'s own `lifespan` already closed `pool`
    # during `TestClient`'s `__exit__` above, in the same event loop the workers were spawned in
    # (see api.py's `lifespan` docstring for why a separate loop here would silently leak every
    # worker). `engine.dispose()` from a different loop than the one that used it is still fine
    # empirically (confirmed: Postgres's own connection count returns to baseline either way).
    asyncio.run(engine.dispose())


def test_check_ok(client: TestClient, registered_base_env: str) -> None:
    response = client.post(
        "/v1/check", json={"base_env_digest": registered_base_env, "body": "def foo : Nat := 5"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "proved"
    assert body["ok"] is True
    assert body["diagnostics"] == []
    assert body["cache_hit"] is False


def test_check_type_error(client: TestClient, registered_base_env: str) -> None:
    response = client.post(
        "/v1/check",
        json={"base_env_digest": registered_base_env, "body": "def bad : Nat := true"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "errors"
    assert body["ok"] is False
    assert body["diagnostics"]


def test_check_second_call_is_a_cache_hit(client: TestClient, registered_base_env: str) -> None:
    req = {"base_env_digest": registered_base_env, "body": "def foo : Nat := 5"}
    first = client.post("/v1/check", json=req).json()
    second = client.post("/v1/check", json=req).json()

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert second["kind"] == first["kind"]
    assert second["ok"] == first["ok"]


def test_check_unknown_base_env_is_404(client: TestClient) -> None:
    response = client.post(
        "/v1/check", json={"base_env_digest": "ab" * 32, "body": "def foo : Nat := 5"}
    )
    assert response.status_code == 404


def test_check_invalid_hex_digest_is_400(client: TestClient) -> None:
    response = client.post(
        "/v1/check", json={"base_env_digest": "not-hex-at-all", "body": "def foo : Nat := 5"}
    )
    assert response.status_code == 400


def test_check_timeout_reports_infra_taxonomy(client: TestClient, registered_base_env: str) -> None:
    response = client.post(
        "/v1/check",
        json={
            "base_env_digest": registered_base_env,
            "body": "partial def spin : IO Unit := spin\n#eval spin",
            "timeout_ms": 1000,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "timeout"
    assert body["ok"] is False


def test_check_batch_amortizes_and_mixes_hits_and_misses(
    client: TestClient, registered_base_env: str
) -> None:
    warm_body = "def already_cached : Nat := 1"
    client.post("/v1/check", json={"base_env_digest": registered_base_env, "body": warm_body})

    response = client.post(
        "/v1/check_batch",
        json={
            "base_env_digest": registered_base_env,
            "bodies": [warm_body, "def freshly_checked : Nat := 2"],
        },
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 2
    assert results[0]["cache_hit"] is True
    assert results[0]["ok"] is True
    assert results[1]["cache_hit"] is False
    assert results[1]["ok"] is True


def test_seal_creates_a_bundle_for_the_goals_that_elaborate(
    client: TestClient, registered_base_env: str
) -> None:
    """Spec §6.1's headline seal behaviour, end to end: a submission whose goals do not all
    elaborate still seals the ones that do, and reports the one that does not."""
    response = client.post(
        "/v1/seal",
        json={
            "base_env_digest": registered_base_env,
            "goals": [
                {"name": "G_ok", "statement": "∀ n : Nat, n + 0 = n"},
                {"name": "G_bad", "statement": "SomeUndefinedThing"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    ok_goal, bad_goal = body["goals"]

    assert ok_goal["ok"] is True
    assert ok_goal["decl_name"] == "LeanAgent.Goals.G_ok"
    assert ok_goal["goal_src"] == "∀ n : Nat, n + 0 = n"
    assert ok_goal["diagnostics"] == []

    assert bad_goal["ok"] is False
    assert bad_goal["diagnostics"]

    # The bundle is compiled out of band with nothing re-verifying it, so the goal that failed to
    # seal must not be in the source that gets compiled -- only the reports mention it.
    assert "G_ok" in body["bundle_source"]
    assert "SomeUndefinedThing" not in body["bundle_source"]
    assert "import Init" in body["bundle_source"]
    assert "set_option autoImplicit false" in body["bundle_source"]


def test_seal_bundle_digest_addresses_the_returned_source(
    client: TestClient, registered_base_env: str
) -> None:
    response = client.post(
        "/v1/seal",
        json={
            "base_env_digest": registered_base_env,
            "goals": [{"name": "G_digest", "statement": "True"}],
        },
    )
    body = response.json()
    assert body["ok"] is True
    assert body["bundle_digest"] == hashlib.sha256(body["bundle_source"].encode()).hexdigest()


def test_seal_goal_digest_ignores_the_declaration_name(
    client: TestClient, registered_base_env: str
) -> None:
    """The same statement under two different names is the same goal. This is what makes spec
    §5.2's cycle guard ("a child's `goal_digest` may not equal any ancestor's") able to fire at
    all -- a digest that folded in the generated declaration name would be unique per obligation
    and the guard would silently never match.
    """
    response = client.post(
        "/v1/seal",
        json={
            "base_env_digest": registered_base_env,
            "goals": [
                {"name": "G_first", "statement": "∀ n : Nat, n + 0 = n"},
                {"name": "G_second", "statement": "∀ n : Nat, n + 0 = n"},
                {"name": "G_other", "statement": "True"},
            ],
        },
    )
    first, second, other = response.json()["goals"]
    assert first["decl_name"] != second["decl_name"]
    assert first["goal_digest"] == second["goal_digest"]
    assert other["goal_digest"] != first["goal_digest"]


def test_seal_rejects_a_statement_that_declares_anything_else(
    client: TestClient, registered_base_env: str
) -> None:
    """`statement` is spliced into generated source, so it is an injection site -- a statement
    that closes the `def` early and appends its own commands must not end up in a bundle that is
    supposed to be an environment the agent cannot influence (spec §1.1)."""
    response = client.post(
        "/v1/seal",
        json={
            "base_env_digest": registered_base_env,
            "goals": [
                {
                    "name": "G_esc",
                    "statement": (
                        "True\nend LeanAgent.Goals\ndef Evil : Nat := 0\nnamespace LeanAgent.Goals"
                    ),
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["goals"][0]["ok"] is False
    assert "Evil" not in body["bundle_source"]


def test_seal_unknown_base_env_is_404(client: TestClient) -> None:
    response = client.post(
        "/v1/seal",
        json={"base_env_digest": "ab" * 32, "goals": [{"name": "G", "statement": "True"}]},
    )
    assert response.status_code == 404


def test_health_reports_pool_shape(client: TestClient, registered_base_env: str) -> None:
    client.post(
        "/v1/check", json={"base_env_digest": registered_base_env, "body": "def foo : Nat := 5"}
    )

    response = client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["idle_workers"] >= 1
    assert body["busy_workers"] == 0
    assert 0.0 <= body["hit_rate"] <= 1.0
    assert isinstance(body["warm_by_base_env"], dict)
