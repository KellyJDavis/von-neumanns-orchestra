"""M2.9: `LeanServiceClient` against the real leanserv app.

Real client code, real server, real kernel, no socket -- `httpx.ASGITransport` routes requests
straight into the ASGI app. That combination is the point: until now the only `LeanService`
implementation was a test adapter, so the code a deployment would actually run had no coverage.

The app is driven through `TestClient` as well, purely so its `lifespan` runs and the worker pool
is closed on the way out (M1.8.5's leak). The `AsyncClient` under test talks to the same app
object.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from conftest import MaterializedBundle
from fastapi.testclient import TestClient
from lean_agent_core.blobs import LocalBlobStore
from lean_agent_core.enums import VerdictKind
from lean_agent_core.protocols import SealGoalRequest
from lean_agent_serv.api import create_app
from lean_agent_serv.cache import VerificationCacheStore
from lean_agent_serv.client import LeanServiceClient
from lean_agent_serv.pool import LeanReplPool, PoolConfig
from lean_agent_serv.verdicts import VerdictWriter
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
def registered_base_env(admin_engine: Engine) -> Iterator[str]:
    digest = f"client-{uuid.uuid4()}".encode()
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO base_env (digest, recipe, toolchain_rev, mathlib_rev) VALUES "
                "(:d, '{\"imports\": [\"Init\"]}', 'v4.33.1', 'deadbeef')"
            ),
            {"d": digest},
        )
        conn.commit()
    yield digest.hex()
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM base_env WHERE digest = :d"), {"d": digest})
        conn.commit()


@pytest.fixture
def leanserv(
    lake_project_dir: Path,
    leanserv_async_database_url: str,
    tmp_path: Path,
    materialized_bundle: MaterializedBundle,
) -> Iterator[TestClient]:
    engine = create_async_engine(leanserv_async_database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    blobs = LocalBlobStore(tmp_path)
    pool = LeanReplPool(
        lake_project_dir, PoolConfig(max_total_workers=4, bundle_root=materialized_bundle.root)
    )
    app = create_app(
        pool,
        VerificationCacheStore(sessionmaker, blobs),
        VerdictWriter(sessionmaker, blobs),
        sessionmaker,
    )
    with TestClient(app) as client:
        yield client
    asyncio.run(engine.dispose())


def _drive(leanserv: TestClient, body: object) -> object:
    """Run one coroutine against a `LeanServiceClient` wired to the app, owning the transport for
    exactly that scope."""

    async def main() -> object:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=leanserv.app), base_url="http://leanserv"
        ) as http:
            return await body(LeanServiceClient(client=http))  # type: ignore[operator]

    return asyncio.run(main())


def test_check_round_trips(leanserv: TestClient, registered_base_env: str) -> None:
    async def body(lean: LeanServiceClient) -> tuple[bool, bool]:
        good = await lean.check(base_env_digest=registered_base_env, body="def a : Nat := 1")
        bad = await lean.check(base_env_digest=registered_base_env, body="def b : Nat := true")
        assert bad.diagnostics
        return good.ok, bad.ok

    assert _drive(leanserv, body) == (True, False)


def test_seal_round_trips_including_level_params(
    leanserv: TestClient, registered_base_env: str
) -> None:
    """`level_params` has to survive the wire: sealing forces `autoImplicit false`, so a statement
    naming a universe fails outright if the field is dropped in transit (M2.1.3)."""

    async def body(lean: LeanServiceClient) -> object:
        return await lean.seal(
            base_env_digest=registered_base_env,
            goals=[
                SealGoalRequest(name="G_ok", statement="∀ n : Nat, n + 0 = n"),
                SealGoalRequest(name="G_poly", statement="PUnit.{w}", level_params=("w",)),
                SealGoalRequest(name="G_bad", statement="NoSuchIdentifier"),
            ],
        )

    sealed = _drive(leanserv, body)
    assert sealed.ok is False  # type: ignore[attr-defined]
    ok_goal, poly_goal, bad_goal = sealed.goals  # type: ignore[attr-defined]
    assert (ok_goal.ok, poly_goal.ok, bad_goal.ok) == (True, True, False)
    assert poly_goal.level_params == ("w",)
    assert "def G_poly.{w} : Sort _ :=" in sealed.bundle_source  # type: ignore[attr-defined]
    # The failed goal is reported and left out of the compilable bundle (M2.1.1).
    assert "NoSuchIdentifier" not in sealed.bundle_source  # type: ignore[attr-defined]


def test_decompose_round_trips(leanserv: TestClient, registered_base_env: str) -> None:
    async def body(lean: LeanServiceClient) -> object:
        return await lean.decompose(
            base_env_digest=registered_base_env,
            development="theorem q (n : Nat) (h : n > 0) : n + 0 = n := by sorry",
        )

    decomposed = _drive(leanserv, body)
    assert decomposed.ok is True  # type: ignore[attr-defined]
    (lemma,) = decomposed.lemmas  # type: ignore[attr-defined]
    assert lemma.statement == "∀ (n : Nat), n > 0 → n + 0 = n"
    assert lemma.round_trips is True
    assert "@sorry_1 n h" in decomposed.reassembly  # type: ignore[attr-defined]


def test_link_round_trips_and_carries_the_verdict_kind(
    admin_engine: Engine,
    leanserv: TestClient,
    registered_base_env: str,
    materialized_bundle: MaterializedBundle,
) -> None:
    """The one method whose response has to survive intact for the state machine to work:
    `VerdictKind` and the three acceptance flags decide whether `mark_proved` will accept."""
    run_id, obligation_id, attempt_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, tenant_id, base_env_digest, status, manifest, manifest_hash)"
                " VALUES (:id, :t, :b, 'running', '{}', :mh)"
            ),
            {
                "id": run_id,
                "t": uuid.uuid4(),
                "b": bytes.fromhex(registered_base_env),
                "mh": b"m",
            },
        )
        conn.execute(
            text(
                "INSERT INTO obligation (id, run_id, base_env_digest, goal_digest, "
                "sealed_olean_sha, goal_src, decl_name, status) VALUES (:id, :r, :b, :g, :s, "
                "'src', 'LeanAgent.Goals.G_add_zero', 'in_progress')"
            ),
            {
                "id": obligation_id,
                "r": run_id,
                "b": bytes.fromhex(registered_base_env),
                "g": f"g-{uuid.uuid4()}".encode(),
                "s": materialized_bundle.olean_digest,
            },
        )
        conn.execute(
            text(
                "INSERT INTO attempt (id, obligation_id, run_id, policy_id, policy_config_hash) "
                "VALUES (:id, :o, :r, 'p', 'c')"
            ),
            {"id": attempt_id, "o": obligation_id, "r": run_id},
        )
        conn.commit()

    async def body(lean: LeanServiceClient) -> object:
        return await lean.link(
            attempt_id=attempt_id,
            obligation_id=obligation_id,
            base_env_digest=registered_base_env,
            bundle_sha=materialized_bundle.sha,
            goal="LeanAgent.Goals.G_add_zero",
            entry="LeanAgent.Sol.sol",
            development=(
                "set_option linter.defProp false\nnamespace LeanAgent.Sol\n"
                "def sol : LeanAgent.Goals.G_add_zero := by "
                "unfold LeanAgent.Goals.G_add_zero; simp\nend LeanAgent.Sol"
            ),
        )

    try:
        outcome = _drive(leanserv, body)
        assert outcome.kind is VerdictKind.PROVED  # type: ignore[attr-defined]
        assert outcome.proved is True  # type: ignore[attr-defined]
        # `simp` really does use `propext` and `Quot.sound` here -- both are in the run's default
        # allowlist, so the audit passes. Asserting the cone is *empty* would have been asserting
        # something about `simp` rather than about the client; what matters is that the reported
        # axioms survive the wire and fall inside the allowlist the audit judged them against.
        assert set(outcome.axioms) <= {  # type: ignore[attr-defined]
            "propext",
            "Classical.choice",
            "Quot.sound",
        }
        assert outcome.axiom_audit_ok is True  # type: ignore[attr-defined]
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": run_id})
            conn.commit()


def test_an_http_error_surfaces_rather_than_being_swallowed(
    leanserv: TestClient,
) -> None:
    """A 404 from leanserv means a real misconfiguration -- an unknown base env, an unmaterialized
    bundle. Turning it into a falsy result would make it indistinguishable from "the goal did not
    check", which is exactly the `infra_error`/proof-failure confusion spec warns about."""

    async def body(lean: LeanServiceClient) -> None:
        with pytest.raises(httpx.HTTPStatusError):
            await lean.check(base_env_digest="ab" * 32, body="def a : Nat := 1")

    _drive(leanserv, body)


def test_a_client_does_not_close_a_transport_it_was_given(leanserv: TestClient) -> None:
    """Ownership matters once a deployment shares one connection pool across several clients:
    closing someone else's would break every other user of it."""

    async def main() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=leanserv.app), base_url="http://leanserv"
        ) as http:
            async with LeanServiceClient(client=http):
                pass
            assert http.is_closed is False

    asyncio.run(main())


def test_a_client_needs_either_a_url_or_a_transport() -> None:
    with pytest.raises(ValueError, match="either `base_url` or `client`"):
        LeanServiceClient()
