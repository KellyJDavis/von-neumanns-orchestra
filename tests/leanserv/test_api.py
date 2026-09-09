"""M1.8.5 exit criterion: the FastAPI surface (`/v1/check`, `/v1/check_batch`, `/v1/health`)
wired to real infrastructure throughout -- a genuinely spawned `leankernel serve` process
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

LEANKERNEL_DIR = Path(__file__).resolve().parents[2] / "packages" / "leankernel"
LEANKERNEL_EXE = LEANKERNEL_DIR / ".lake" / "build" / "bin" / "leankernel"


@pytest.fixture(scope="session")
def lake_project_dir() -> Path:
    if not LEANKERNEL_EXE.exists():
        pytest.skip(
            f"{LEANKERNEL_EXE} not built; run `lake build` in {LEANKERNEL_DIR} first. "
            "CI always builds it before this suite runs (see .github/workflows/ci.yml)."
        )
    return LEANKERNEL_DIR


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

    asyncio.run(pool.aclose())
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
