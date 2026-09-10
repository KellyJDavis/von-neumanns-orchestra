"""M2.8 exit criterion: the public API (spec §6.1) over the real pipeline.

Every endpoint runs against a real PostgreSQL and, where it reaches Lean, a real kernel: `POST
/v1/runs` genuinely ingests, seals and materializes, and `GET /v1/runs/{id}/artifact` genuinely
assembles. Nothing here is stubbed except the transport into leanserv, which is the same
`TestClient`-backed `LeanService` M2.5 introduced.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lean_agent_api.app import create_app
from lean_agent_api.materialize import BundleMaterializer
from lean_agent_core.actions import Budget, ObligationContext
from lean_agent_core.blobs import LocalBlobStore
from lean_agent_core.executor import PolicyExecutor, TrajectoryWriter
from lean_agent_core.scheduler import ClaimedAttempt, claim_attempt
from lean_agent_core.state import ObligationOutcome, ObligationStateMachine
from lean_agent_policies.symbolic import SymbolicPortfolio
from lean_agent_serv.api import create_app as create_leanserv_app
from lean_agent_serv.cache import VerificationCacheStore
from lean_agent_serv.pool import LeanReplPool, PoolConfig
from lean_agent_serv.verdicts import VerdictWriter
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_end_to_end import LeanServiceOverTestClient


@pytest.fixture
def bundle_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("api_bundles")


@pytest.fixture
def base_env(admin_engine: Engine) -> Iterator[str]:
    digest = f"api-{uuid.uuid4()}".encode()
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
    bundle_root: Path,
) -> Iterator[TestClient]:
    engine = create_async_engine(leanserv_async_database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    blobs = LocalBlobStore(tmp_path / "leanserv_blobs")
    pool = LeanReplPool(lake_project_dir, PoolConfig(max_total_workers=4, bundle_root=bundle_root))
    app = create_leanserv_app(
        pool,
        VerificationCacheStore(sessionmaker, blobs),
        VerdictWriter(sessionmaker, blobs),
        sessionmaker,
    )
    with TestClient(app) as client:
        yield client
    asyncio.run(engine.dispose())


@pytest.fixture
def api(
    leanserv: TestClient,
    app_async_database_url: str,
    bundle_root: Path,
    lake_project_dir: Path,
    tmp_path: Path,
) -> Iterator[TestClient]:
    """The public API over the real pipeline. Its own engine, disposed inside `TestClient`'s
    lifetime rather than after (M2.2's asyncpg loop-affinity finding)."""
    engine = create_async_engine(app_async_database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    blobs = LocalBlobStore(tmp_path / "api_blobs")
    _APP_URL[0] = app_async_database_url
    _LEAN.clear()
    _LEAN.append(LeanServiceOverTestClient(leanserv))
    _BLOBS.clear()
    _BLOBS.append(blobs)
    app = create_app(
        session_factory=sessions,
        lean=_LEAN[0],
        blobs=blobs,
        materializer=BundleMaterializer(
            session_factory=sessions,
            blobs=blobs,
            bundle_root=bundle_root,
            lake_project_dir=lake_project_dir,
        ),
    )
    with TestClient(app) as client:
        yield client
    asyncio.run(engine.dispose())


def _cleanup(admin_engine: Engine, run_id: str) -> None:
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": uuid.UUID(run_id)})
        conn.commit()


def _create_run(api: TestClient, base_env: str, **kwargs: object) -> dict[str, object]:
    response = api.post("/v1/runs", json={"base_env": base_env, **kwargs})
    assert response.status_code == 201, response.text
    return dict(response.json())


#: Set by the `api` fixture so `_prove_one` and the cancel test can open their own connections --
#: they need the `app` role directly, not through the HTTP surface.
_APP_URL: list[str] = [""]


def _prove_one(app_async_database_url: str, api: TestClient, obligation_id: str) -> bool:
    """Claim and prove one obligation with the null agent, so the read endpoints have a real
    attempt, verdict and trajectory to return. Uses the same components the control loop does."""

    async def main() -> bool:
        engine = create_async_engine(app_async_database_url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            policy = SymbolicPortfolio(tactics=("decide", "simp"))
            claimed = await claim_attempt(
                sessions,
                worker_id="api-test",
                policy_id=policy.id,
                policy_config_hash=policy.config_hash,
            )
            if claimed is None or str(claimed.obligation_id) != obligation_id:
                return False
            async with sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT base_env_digest, bundle_sha, decl_name, goal_src "
                            "FROM obligation WHERE id = :id"
                        ),
                        {"id": claimed.obligation_id},
                    )
                ).one()
            ctx = ObligationContext(
                obligation_id=claimed.obligation_id,
                run_id=claimed.run_id,
                base_env_digest=bytes(row[0]).hex(),
                bundle_sha=bytes(row[1]).hex(),
                goal_decl=row[2],
                goal_src=row[3],
                entry=row[2].replace("LeanAgent.Goals.G_", "LeanAgent.Sol.sol_", 1),
            )
            executor = PolicyExecutor(
                policy=policy,
                lean=_LEAN[0],
                trajectories=TrajectoryWriter(sessions, _BLOBS[0]),
                context_loader=lambda o, r: _fixed(ctx),
            )
            result = await executor.execute(
                attempt_id=claimed.attempt_id, ctx=ctx, budget=Budget(attempts_remaining=8)
            )
            if result.outcome is not ObligationOutcome.PROVED:
                return False
            await _finish(sessions, claimed)
            await ObligationStateMachine(sessions).mark_proved(
                claimed.obligation_id, claimed.attempt_id
            )
            return True
        finally:
            await engine.dispose()

    del api
    return asyncio.run(main())


async def _fixed(ctx: ObligationContext) -> tuple[ObligationContext, Budget]:
    return ctx, Budget(attempts_remaining=8)


async def _finish(sessions: async_sessionmaker[AsyncSession], claimed: ClaimedAttempt) -> None:
    """Close the attempt the way the control loop would, so `/v1/attempts` returns a finished row
    rather than one still marked `claimed`."""
    async with sessions() as session:
        await session.execute(
            text(
                "UPDATE attempt SET status = 'succeeded', finished_at = now(), lease_owner = NULL "
                "WHERE id = :id"
            ),
            {"id": claimed.attempt_id},
        )
        await session.commit()


#: Filled by the `api` fixture; the helpers above need the same `LeanService` and blob store the
#: app was built with, and threading them through every test signature would drown the assertions.
_LEAN: list[LeanServiceOverTestClient] = []
_BLOBS: list[LocalBlobStore] = []


def test_create_run_ingests_seals_and_materializes(
    admin_engine: Engine, api: TestClient, base_env: str
) -> None:
    """Spec §6.1's `POST /v1/runs`, doing all of §6.3 steps 1-4 plus the bundle build.

    Materializing synchronously here is deliberate: §4.1 keeps the build off the *hot path* (a
    check), not off submission, and a caller handed a 201 with obligation ids should be able to act
    on them. A run whose bundle is never compiled can never prove anything.
    """
    body = _create_run(api, base_env, statement="(2 : Nat) + 2 = 4")
    try:
        (obligation_id,) = body["root_obligations"]
        assert body["seal_failures"] == []
        assert body["admission"][obligation_id]["closed_by"] in {"simp", "decide"}

        detail = api.get(f"/v1/obligations/{obligation_id}").json()
        assert detail["decl_name"] == "LeanAgent.Goals.G_1"
        assert detail["status"] == "open"
        assert detail["is_root"] is True
        # Materialized: both digests present, which is what makes the obligation provable at all.
        assert detail["bundle_sha"] is not None
        assert detail["sealed_olean_sha"] is not None
    finally:
        _cleanup(admin_engine, str(body["run_id"]))


def test_create_run_reports_seal_failures_rather_than_failing(
    admin_engine: Engine, api: TestClient, base_env: str
) -> None:
    """Spec §6.1: `seal_failures` "is returned rather than raised"."""
    body = _create_run(api, base_env, statement="NoSuchIdentifier")
    try:
        assert body["root_obligations"] == []
        (failure,) = body["seal_failures"]
        assert failure["reason"] == "did not elaborate"
    finally:
        _cleanup(admin_engine, str(body["run_id"]))


def test_create_run_requires_exactly_one_of_source_or_statement(
    api: TestClient, base_env: str
) -> None:
    """Enforced by the Pydantic model, so it is a 422 from the generated schema rather than a
    hand-written check that could drift from the OpenAPI document."""
    for payload in ({}, {"source": "theorem t : True := by sorry", "statement": "True"}):
        response = api.post("/v1/runs", json={"base_env": base_env, **payload})
        assert response.status_code == 422


def test_create_run_unknown_base_env_is_404(api: TestClient) -> None:
    response = api.post("/v1/runs", json={"base_env": "ab" * 32, "statement": "True"})
    assert response.status_code == 404


def test_run_status_counts_obligations_and_sums_spend(
    admin_engine: Engine, api: TestClient, base_env: str
) -> None:
    body = _create_run(
        api,
        base_env,
        source=(
            "theorem both : (1 : Nat) + 1 = 2 ∧ (2 : Nat) + 2 = 4 := by\n"
            "  constructor\n  · sorry\n  · sorry"
        ),
    )
    try:
        status = api.get(f"/v1/runs/{body['run_id']}").json()
        assert status["status"] == "running"
        assert status["obligations_by_status"] == {"open": 2}
        assert status["spend"] == {"tokens": 0, "kernel_ms": 0, "attempts": 0}
        assert status["manifest_hash"] == body["manifest_hash"]
    finally:
        _cleanup(admin_engine, str(body["run_id"]))


def test_manifest_is_the_frozen_record(
    admin_engine: Engine, api: TestClient, base_env: str
) -> None:
    """Spec §7.3. `models: []` is the claim a published Phase 2 result has to make -- an absent key
    and an empty list say different things."""
    body = _create_run(api, base_env, statement="True")
    try:
        manifest = api.get(f"/v1/runs/{body['run_id']}/manifest").json()
        assert manifest["models"] == []
        assert manifest["axiom_allowlist"] == ["propext", "Classical.choice", "Quot.sound"]
        assert manifest["allow_sorry"] is False
    finally:
        _cleanup(admin_engine, str(body["run_id"]))


def test_obligations_are_paginated_and_filterable(
    admin_engine: Engine, api: TestClient, base_env: str
) -> None:
    body = _create_run(
        api,
        base_env,
        source=(
            "theorem three : (1 : Nat) + 1 = 2 ∧ (2 : Nat) + 2 = 4 ∧ (3 : Nat) + 3 = 6 := by\n"
            "  refine ⟨?_, ?_, ?_⟩\n  · sorry\n  · sorry\n  · sorry"
        ),
    )
    try:
        run_id = body["run_id"]
        first = api.get(f"/v1/runs/{run_id}/obligations?limit=2").json()
        assert len(first["obligations"]) == 2
        assert first["next_cursor"] is not None

        second = api.get(
            f"/v1/runs/{run_id}/obligations?limit=2&cursor={first['next_cursor']}"
        ).json()
        # Keyset, not OFFSET: the second page continues past the first rather than re-reading it.
        ids = {o["id"] for o in first["obligations"]} | {o["id"] for o in second["obligations"]}
        assert len(ids) == 3
        assert second["next_cursor"] is None

        assert api.get(f"/v1/runs/{run_id}/obligations?status=proved").json()["obligations"] == []
        assert len(api.get(f"/v1/runs/{run_id}/obligations?depth=0").json()["obligations"]) == 3
    finally:
        _cleanup(admin_engine, str(body["run_id"]))


def test_cancel_stops_further_claims(admin_engine: Engine, api: TestClient, base_env: str) -> None:
    """Spec §6.1: "Cooperative cancel; live attempts finish or expire".

    Setting `run.status` *is* the mechanism -- `claim_attempt` requires `r.status = 'running'` --
    so this asserts the scheduling consequence, not just the column.
    """
    body = _create_run(api, base_env, statement="(2 : Nat) + 2 = 4")
    try:
        assert api.post(f"/v1/runs/{body['run_id']}/cancel").status_code == 202
        assert api.get(f"/v1/runs/{body['run_id']}").json()["status"] == "cancelled"
        # Cancelling twice is a 409, not a second cancel: the run is no longer running.
        assert api.post(f"/v1/runs/{body['run_id']}/cancel").status_code == 409

        async def claim() -> object:
            engine = create_async_engine(_APP_URL[0])
            try:
                return await claim_attempt(
                    async_sessionmaker(engine, expire_on_commit=False),
                    worker_id="w",
                    policy_id="p",
                    policy_config_hash=b"c",
                )
            finally:
                await engine.dispose()

        assert asyncio.run(claim()) is None
    finally:
        _cleanup(admin_engine, str(body["run_id"]))


def test_artifact_assembles_the_materialized_file(
    admin_engine: Engine, api: TestClient, base_env: str
) -> None:
    """Spec §6.1's `/artifact`, over §6.3 step 6. Unproved here, so `complete` is false and the
    open hole is named -- a partial artifact is the useful thing to hand back."""
    body = _create_run(api, base_env, source="theorem t : (1 : Nat) + 1 = 2 := by sorry")
    try:
        artifact = api.get(f"/v1/runs/{body['run_id']}/artifact").json()
        assert artifact["holes"] == 1
        assert artifact["unfilled"] == ["sorry_1"]
        assert artifact["complete"] is False
        assert "theorem t :" in artifact["source"]
        assert "UNPROVED" in artifact["source"]
    finally:
        _cleanup(admin_engine, str(body["run_id"]))


def test_artifact_for_a_bare_statement_is_a_409(
    admin_engine: Engine, api: TestClient, base_env: str
) -> None:
    """A statement submission has no file with holes in it, so there is nothing to assemble --
    a conflict with the request, not a missing resource."""
    body = _create_run(api, base_env, statement="True")
    try:
        assert api.get(f"/v1/runs/{body['run_id']}/artifact").status_code == 409
    finally:
        _cleanup(admin_engine, str(body["run_id"]))


def test_dag_returns_the_sub_dag_with_groups_and_roles(
    admin_engine: Engine, api: TestClient, base_env: str
) -> None:
    """Spec §6.1: "Sub-DAG with groups and edge roles". A root with no children is a one-node DAG,
    which is what ingestion produces today -- edges appear when a policy decomposes."""
    body = _create_run(api, base_env, statement="True")
    try:
        (obligation_id,) = body["root_obligations"]
        dag = api.get(f"/v1/obligations/{obligation_id}/dag").json()
        assert dag["root"] == obligation_id
        assert [n["id"] for n in dag["nodes"]] == [obligation_id]
        assert dag["edges"] == []

        # Now give it a child, through the real edge table, and confirm the walk finds it.
        child = uuid.uuid4()
        group = uuid.uuid4()
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO obligation (id, run_id, base_env_digest, goal_digest, goal_src, "
                    "decl_name, depth) SELECT :c, run_id, base_env_digest, :gd, 'src', "
                    "'LeanAgent.Goals.G_9', 1 FROM obligation WHERE id = :p"
                ),
                {"c": child, "gd": f"g-{uuid.uuid4()}".encode(), "p": uuid.UUID(obligation_id)},
            )
            conn.execute(
                text(
                    "INSERT INTO obligation_edge (parent_id, child_id, group_id, role) "
                    "VALUES (:p, :c, :g, 'subgoal')"
                ),
                {"p": uuid.UUID(obligation_id), "c": child, "g": group},
            )
            conn.commit()

        dag = api.get(f"/v1/obligations/{obligation_id}/dag").json()
        assert {n["id"] for n in dag["nodes"]} == {obligation_id, str(child)}
        (edge,) = dag["edges"]
        assert (edge["child_id"], edge["group_id"], edge["role"]) == (
            str(child),
            str(group),
            "subgoal",
        )
    finally:
        _cleanup(admin_engine, str(body["run_id"]))


def test_attempts_and_trajectory_are_readable_after_a_real_proof(
    admin_engine: Engine, api: TestClient, base_env: str, app_async_database_url: str
) -> None:
    """The read side of everything the null agent writes: the attempt, its verdict, and the
    trajectory whose `provenance` decides whether it may ever be exported as training data."""
    body = _create_run(api, base_env, statement="(2 : Nat) + 2 = 4")
    try:
        (obligation_id,) = body["root_obligations"]
        proved = _prove_one(app_async_database_url, api, obligation_id)
        assert proved is True

        attempts = api.get(f"/v1/obligations/{obligation_id}/attempts").json()
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt["status"] == "succeeded"
        assert attempt["verdict"]["kind"] == "proved"
        assert attempt["verdict"]["link_ok"] is True

        assert api.get(f"/v1/attempts/{attempt['id']}").json()["id"] == attempt["id"]

        trajectory = api.get(f"/v1/attempts/{attempt['id']}/trajectory").json()
        assert trajectory["provenance"] == "symbolic"
        assert trajectory["model_id"] is None
        assert trajectory["n_steps"] == len(trajectory["steps"])
        assert any(step["ok"] for step in trajectory["steps"])

        assert api.get(f"/v1/obligations/{obligation_id}").json()["status"] == "proved"
    finally:
        _cleanup(admin_engine, str(body["run_id"]))


def test_base_envs_are_listable_and_registrable(api: TestClient, base_env: str) -> None:
    listed = api.get("/v1/base-envs").json()
    assert any(env["digest"] == base_env for env in listed)

    created = api.post(
        "/v1/base-envs",
        json={"imports": ["Init"], "toolchain_rev": "v4.33.1", "mathlib_rev": "abc"},
    )
    assert created.status_code == 201
    first = created.json()
    # Content-addressed: registering the same prelude twice is the same environment, not two --
    # which matters directly for the warm-worker pool, where a duplicate digest would cost a
    # second full memory slot for an identical environment.
    again = api.post(
        "/v1/base-envs",
        json={"imports": ["Init"], "toolchain_rev": "v4.33.1", "mathlib_rev": "abc"},
    ).json()
    assert again["digest"] == first["digest"]
    # And a caller cannot mark its own prelude curated, which would opt it into the hot pool.
    assert first["curated"] is False


def test_a_tenant_scoped_blob_is_refused_rather_than_served(
    admin_engine: Engine, api: TestClient
) -> None:
    """Spec marks `/v1/blobs/{sha}` tenant-scoped, and multi-tenancy is post-MVP. Serving a
    tenant-owned blob from an endpoint that cannot authenticate a tenant would be the wrong way to
    round that gap."""
    digest = uuid.uuid4().bytes * 2
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO blob (sha256, size_bytes, media_type, location, tenant_id) "
                "VALUES (:d, 1, 'text/plain', 'file:///nowhere', :t)"
            ),
            {"d": digest, "t": uuid.uuid4()},
        )
        conn.commit()
    try:
        assert api.get(f"/v1/blobs/{digest.hex()}").status_code == 403
        assert api.get("/v1/blobs/not-hex").status_code == 400
        assert api.get(f"/v1/blobs/{'ab' * 32}").status_code == 404
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text("DELETE FROM blob WHERE sha256 = :d"), {"d": digest})
            conn.commit()


def test_liveness_readiness_and_metrics(
    admin_engine: Engine, api: TestClient, base_env: str
) -> None:
    """`/healthz` touches nothing else on purpose -- a liveness probe that fails during someone
    else's outage gets this process killed for it. `/readyz` does touch the database, because an
    API that cannot reach Postgres should leave the rotation.

    The metrics assertion creates a run first: asserting only on the `# TYPE` header would pass
    against an endpoint that emitted headers and no series at all.
    """
    assert api.get("/healthz").json() == {"status": "ok"}
    assert api.get("/readyz").json() == {"status": "ready"}

    body = _create_run(api, base_env, statement="True")
    try:
        metrics = api.get("/metrics").text
        assert "# TYPE lean_agent_obligations gauge" in metrics
        assert 'lean_agent_runs{status="running"}' in metrics
        assert 'lean_agent_obligations{status="open"}' in metrics
    finally:
        _cleanup(admin_engine, str(body["run_id"]))


def test_events_stream_emits_a_snapshot_then_closes_on_a_finished_run(
    admin_engine: Engine, api: TestClient, base_env: str
) -> None:
    """SSE. A subscriber joining mid-run gets a `snapshot` first rather than guessing at the state
    it missed, and the stream ends when the run does instead of polling a finished run forever."""
    body = _create_run(api, base_env, statement="True")
    try:
        api.post(f"/v1/runs/{body['run_id']}/cancel")
        with api.stream("GET", f"/v1/runs/{body['run_id']}/events") as stream:
            payload = "".join(stream.iter_text())
        assert "event: snapshot" in payload
        assert "event: run" in payload
        assert '"status": "cancelled"' in payload
    finally:
        _cleanup(admin_engine, str(body["run_id"]))


def test_the_openapi_document_is_generated(api: TestClient) -> None:
    """Spec §6.1: "the OpenAPI document is generated, not written". Every §6.1 path must appear in
    it, which is also the cheapest check that none was quietly dropped."""
    paths = set(api.get("/openapi.json").json()["paths"])
    for path in (
        "/v1/runs",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/obligations",
        "/v1/runs/{run_id}/events",
        "/v1/runs/{run_id}/artifact",
        "/v1/runs/{run_id}/manifest",
        "/v1/runs/{run_id}/cancel",
        "/v1/obligations/{obligation_id}",
        "/v1/obligations/{obligation_id}/dag",
        "/v1/obligations/{obligation_id}/attempts",
        "/v1/attempts/{attempt_id}",
        "/v1/attempts/{attempt_id}/trajectory",
        "/v1/base-envs",
        "/v1/blobs/{sha256}",
        "/healthz",
        "/readyz",
        "/metrics",
    ):
        assert path in paths, path
