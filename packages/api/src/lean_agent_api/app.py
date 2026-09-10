"""The public API (spec §6.1), FastAPI.

"All request and response bodies are Pydantic models; the OpenAPI document is generated, not
written." This module wires §6.1's table to the machinery M2.6/M2.7 built -- ingestion,
materialization, the obligation DAG -- and adds nothing of its own beyond reading rows.

`create_app` is a factory for the same reason `leanserv`'s is: `lean`, `blobs` and the session
factory are real, expensive, stateful resources a deployment constructs, not things a module
should reach for.

Two endpoints in §6.1's table need naming for what they are rather than what they might look like:

* `GET /v1/runs/{id}/events` streams state transitions by **polling** the obligation statuses and
  emitting deltas. `LISTEN`/`NOTIFY` would push instead, and would mean a dedicated connection per
  subscriber held open for the life of a run -- the same objection spec raises against session-level
  advisory locks for leases ("pin one backend connection per in-flight attempt ... to buy seconds
  of detection latency"). A poll is honest about its latency and costs one query per interval.
* `GET /metrics` emits Prometheus text exposition directly rather than through `prometheus_client`.
  The numbers here are all counts of rows, so a client library would add a dependency and a
  registry to format eight lines of text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from lean_agent_core.blobs import from_bytea
from lean_agent_core.protocols import BlobStore, LeanService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lean_agent_api.ingestion import Ingestor, Submission
from lean_agent_api.materialize import BundleMaterializer, FileMaterializer, MaterializationError
from lean_agent_api.schemas import (
    AdmissionBody,
    ArtifactResponse,
    AttemptSummary,
    BaseEnvBody,
    CreateRunRequest,
    CreateRunResponse,
    DagEdge,
    DagResponse,
    ObligationDetail,
    ObligationPage,
    ObligationSummary,
    RegisterBaseEnvRequest,
    RunSpend,
    RunStatusResponse,
    SealFailureBody,
    TrajectoryResponse,
    VerdictBody,
)

#: How often the SSE stream re-reads a run's obligation statuses. Two seconds is a deliberate
#: compromise: fast enough that a UI feels live, slow enough that a hundred subscribers are a
#: hundred queries every two seconds rather than a hundred open connections.
EVENT_POLL_SECONDS = 2.0


def _hex(value: bytes | None) -> str | None:
    return bytes(value).hex() if value is not None else None


async def _one(
    sessions: async_sessionmaker[AsyncSession], sql: str, params: dict[str, Any], missing: str
) -> Any:
    async with sessions() as session:
        row = (await session.execute(text(sql), params)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=missing)
    return row


def create_app(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    lean: LeanService,
    blobs: BlobStore,
    materializer: BundleMaterializer | None = None,
) -> FastAPI:
    """`materializer` is optional because a read-only deployment of this API (a status dashboard)
    needs no bundle root and no Lake project. `POST /v1/runs` refuses rather than half-works when
    it is absent: a run whose bundle is never compiled can never prove anything (M2.7), so
    accepting the submission would be accepting work that cannot complete."""
    app = FastAPI(
        title="von-neumann's-orchestra",
        description="Public API (spec §6.1)",
    )
    ingestor = Ingestor(session_factory=session_factory, lean=lean, blobs=blobs)
    files = FileMaterializer(session_factory=session_factory, blobs=blobs, lean=lean)

    # --- runs ---------------------------------------------------------------------------------

    @app.post("/v1/runs", response_model=CreateRunResponse, status_code=201)
    async def create_run(req: CreateRunRequest) -> CreateRunResponse:
        if materializer is None:
            raise HTTPException(
                status_code=503,
                detail="this deployment has no bundle materializer, so a submission's goals could "
                "never be linked against (see M2.7); refusing rather than accepting work that "
                "cannot complete",
            )
        submission = Submission(
            base_env_digest=req.base_env,
            # Multi-tenancy is post-MVP (spec §9), so every submission lands in one tenant rather
            # than the API pretending to authenticate one it has no way to check.
            tenant_id=uuid.UUID(int=0),
            source=req.source,
            statement=req.statement,
            policy=req.policy,
            allow_sorry=req.allow_sorry,
            axiom_allowlist=tuple(
                req.axiom_allowlist
                if req.axiom_allowlist is not None
                else ("propext", "Classical.choice", "Quot.sound")
            ),
            max_depth=req.max_depth,
            budget_attempts=req.budget_attempts,
            budget_tokens=req.budget_tokens,
            budget_kernel_ms=req.budget_kernel_ms,
        )
        try:
            result = await ingestor.ingest(submission)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # Materialized here, synchronously, rather than left to a background sweeper: spec §4.1
        # keeps the build off the *hot path* (a check), not off submission, and a caller that gets
        # a 201 should be able to act on the obligations it names.
        if result.root_obligations:
            await materializer.materialize_run(result.run_id)

        return CreateRunResponse(
            run_id=result.run_id,
            manifest_hash=result.manifest_hash,
            root_obligations=result.root_obligations,
            admission={
                oid: AdmissionBody(**report.as_json()) for oid, report in result.admission.items()
            },
            seal_failures=[
                SealFailureBody(
                    name=f.name,
                    statement=f.statement,
                    reason=f.reason,
                    diagnostics=list(f.diagnostics),
                )
                for f in result.seal_failures
            ],
        )

    @app.get("/v1/runs/{run_id}", response_model=RunStatusResponse)
    async def get_run(run_id: uuid.UUID) -> RunStatusResponse:
        row = await _one(
            session_factory,
            "SELECT status, created_at, manifest_hash FROM run WHERE id = :id",
            {"id": run_id},
            f"no run {run_id}",
        )
        async with session_factory() as session:
            counts = (
                await session.execute(
                    text(
                        "SELECT status::text, count(*) FROM obligation WHERE run_id = :id "
                        "GROUP BY status"
                    ),
                    {"id": run_id},
                )
            ).all()
            spend = (
                await session.execute(
                    text(
                        "SELECT coalesce(sum(spent_tokens), 0), coalesce(sum(spent_kernel_ms), 0),"
                        " coalesce(sum(spent_attempts), 0) FROM obligation WHERE run_id = :id"
                    ),
                    {"id": run_id},
                )
            ).one()
        return RunStatusResponse(
            run_id=run_id,
            status=row[0],
            created_at=row[1].isoformat(),
            manifest_hash=bytes(row[2]).hex(),
            obligations_by_status={status: int(count) for status, count in counts},
            spend=RunSpend(tokens=int(spend[0]), kernel_ms=int(spend[1]), attempts=int(spend[2])),
        )

    @app.get("/v1/runs/{run_id}/manifest")
    async def get_manifest(run_id: uuid.UUID) -> dict[str, Any]:
        row = await _one(
            session_factory,
            "SELECT manifest FROM run WHERE id = :id",
            {"id": run_id},
            f"no run {run_id}",
        )
        return dict(row[0])

    @app.get("/v1/runs/{run_id}/obligations", response_model=ObligationPage)
    async def list_obligations(
        run_id: uuid.UUID,
        status: str | None = None,
        depth: int | None = None,
        limit: int = Query(default=100, le=1000),
        cursor: str | None = None,
    ) -> ObligationPage:
        clauses = ["run_id = :run"]
        params: dict[str, Any] = {"run": run_id, "limit": limit}
        if status is not None:
            clauses.append("status = CAST(:status AS obligation_status)")
            params["status"] = status
        if depth is not None:
            clauses.append("depth = :depth")
            params["depth"] = depth
        if cursor is not None:
            clauses.append("id > :cursor")
            params["cursor"] = uuid.UUID(cursor)
        async with session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, status::text, depth, priority, is_root, decl_name, goal_src "
                        f"FROM obligation WHERE {' AND '.join(clauses)} ORDER BY id LIMIT :limit"
                    ),
                    params,
                )
            ).all()
        obligations = [
            ObligationSummary(
                id=r[0],
                status=r[1],
                depth=r[2],
                priority=r[3],
                is_root=r[4],
                decl_name=r[5],
                goal_src=r[6],
            )
            for r in rows
        ]
        return ObligationPage(
            obligations=obligations,
            next_cursor=str(obligations[-1].id) if len(obligations) == limit else None,
        )

    @app.get("/v1/runs/{run_id}/artifact", response_model=ArtifactResponse)
    async def get_artifact(run_id: uuid.UUID) -> ArtifactResponse:
        try:
            assembled = await files.assemble(run_id)
        except MaterializationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ArtifactResponse(
            run_id=run_id,
            source=assembled.source,
            complete=assembled.complete,
            elaborates=assembled.elaborates,
            links=assembled.links,
            holes=assembled.holes,
            unfilled=list(assembled.unfilled),
            diagnostics=list(assembled.diagnostics),
        )

    @app.post("/v1/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: uuid.UUID) -> dict[str, str]:
        """Spec §6.1: "Cooperative cancel; live attempts finish or expire".

        Setting `run.status` is the entire mechanism, and that is not a shortcut: `claim_attempt`
        requires `r.status = 'running'`, so no further attempt is ever claimed. Live attempts are
        left alone deliberately -- killing one discards work that may be seconds from a verdict,
        and the lease already bounds how long a cancelled run can hold a worker.
        """
        async with session_factory() as session:
            cancelled = (
                await session.execute(
                    text(
                        "UPDATE run SET status = 'cancelled' WHERE id = :id AND status = 'running' "
                        "RETURNING id"
                    ),
                    {"id": run_id},
                )
            ).scalar_one_or_none()
            await session.commit()
        if cancelled is None:
            raise HTTPException(status_code=409, detail=f"run {run_id} is not running")
        return {"run_id": str(run_id), "status": "cancelled"}

    @app.get("/v1/runs/{run_id}/events")
    async def stream_events(run_id: uuid.UUID) -> StreamingResponse:
        """SSE stream of obligation state transitions.

        Emits a `snapshot` first so a subscriber joining mid-run is not left guessing at the state
        it missed, then one `transition` per obligation whose status actually changed. Polling, not
        `LISTEN`/`NOTIFY` -- see this module's docstring on why a connection per subscriber is the
        wrong trade.
        """

        async def events() -> AsyncIterator[str]:
            seen: dict[str, str] = {}
            first = True
            while True:
                async with session_factory() as session:
                    rows = (
                        await session.execute(
                            text(
                                "SELECT id, status::text FROM obligation WHERE run_id = :id "
                                "ORDER BY id"
                            ),
                            {"id": run_id},
                        )
                    ).all()
                    run_status = (
                        await session.execute(
                            text("SELECT status FROM run WHERE id = :id"), {"id": run_id}
                        )
                    ).scalar_one_or_none()
                current = {str(r[0]): r[1] for r in rows}
                if first:
                    yield _sse("snapshot", {"run_id": str(run_id), "obligations": current})
                    first = False
                else:
                    for oid, status in current.items():
                        if seen.get(oid) != status:
                            yield _sse(
                                "transition",
                                {"obligation_id": oid, "from": seen.get(oid), "to": status},
                            )
                seen = current
                if run_status != "running":
                    yield _sse("run", {"run_id": str(run_id), "status": run_status})
                    return
                await asyncio.sleep(EVENT_POLL_SECONDS)

        return StreamingResponse(events(), media_type="text/event-stream")

    # --- obligations, attempts, trajectories ---------------------------------------------------

    @app.get("/v1/obligations/{obligation_id}", response_model=ObligationDetail)
    async def get_obligation(obligation_id: uuid.UUID) -> ObligationDetail:
        row = await _one(
            session_factory,
            "SELECT id, status::text, depth, priority, is_root, decl_name, goal_src, run_id, "
            "admission, budget_attempts, spent_attempts, spent_tokens, spent_kernel_ms, "
            "bundle_sha, sealed_olean_sha FROM obligation WHERE id = :id",
            {"id": obligation_id},
            f"no obligation {obligation_id}",
        )
        return ObligationDetail(
            id=row[0],
            status=row[1],
            depth=row[2],
            priority=row[3],
            is_root=row[4],
            decl_name=row[5],
            goal_src=row[6],
            run_id=row[7],
            admission=AdmissionBody(**row[8]),
            budget_attempts=row[9],
            spent_attempts=row[10],
            spent_tokens=row[11],
            spent_kernel_ms=row[12],
            bundle_sha=_hex(row[13]),
            sealed_olean_sha=_hex(row[14]),
        )

    @app.get("/v1/obligations/{obligation_id}/dag", response_model=DagResponse)
    async def get_dag(obligation_id: uuid.UUID) -> DagResponse:
        """The sub-DAG rooted here, walked downward with a recursive CTE.

        Bounded by `run.max_depth` in practice and by the cycle guard in principle: the guard
        refuses an edge whose child repeats an ancestor's `goal_digest`, so this walk cannot loop.
        """
        async with session_factory() as session:
            nodes = (
                await session.execute(
                    text(
                        "WITH RECURSIVE sub(id) AS ("
                        "  SELECT CAST(:root AS uuid) "
                        "  UNION "
                        "  SELECT e.child_id FROM obligation_edge e JOIN sub ON e.parent_id = sub.id"
                        ") SELECT o.id, o.status::text, o.depth, o.priority, o.is_root, "
                        "o.decl_name, o.goal_src FROM obligation o JOIN sub ON sub.id = o.id"
                    ),
                    {"root": obligation_id},
                )
            ).all()
            if not nodes:
                raise HTTPException(status_code=404, detail=f"no obligation {obligation_id}")
            edges = (
                await session.execute(
                    text(
                        "WITH RECURSIVE sub(id) AS ("
                        "  SELECT CAST(:root AS uuid) "
                        "  UNION "
                        "  SELECT e.child_id FROM obligation_edge e JOIN sub ON e.parent_id = sub.id"
                        ") SELECT e.parent_id, e.child_id, e.group_id, e.role "
                        "FROM obligation_edge e JOIN sub ON sub.id = e.parent_id"
                    ),
                    {"root": obligation_id},
                )
            ).all()
        return DagResponse(
            root=obligation_id,
            nodes=[
                ObligationSummary(
                    id=n[0],
                    status=n[1],
                    depth=n[2],
                    priority=n[3],
                    is_root=n[4],
                    decl_name=n[5],
                    goal_src=n[6],
                )
                for n in nodes
            ],
            edges=[DagEdge(parent_id=e[0], child_id=e[1], group_id=e[2], role=e[3]) for e in edges],
        )

    @app.get("/v1/obligations/{obligation_id}/attempts", response_model=list[AttemptSummary])
    async def list_attempts(obligation_id: uuid.UUID) -> list[AttemptSummary]:
        return await _attempts(session_factory, "a.obligation_id = :id", {"id": obligation_id})

    @app.get("/v1/attempts/{attempt_id}", response_model=AttemptSummary)
    async def get_attempt(attempt_id: uuid.UUID) -> AttemptSummary:
        found = await _attempts(session_factory, "a.id = :id", {"id": attempt_id})
        if not found:
            raise HTTPException(status_code=404, detail=f"no attempt {attempt_id}")
        return found[0]

    @app.get("/v1/attempts/{attempt_id}/trajectory", response_model=TrajectoryResponse)
    async def get_trajectory(attempt_id: uuid.UUID) -> TrajectoryResponse:
        row = await _one(
            session_factory,
            "SELECT provenance::text, model_id, n_steps, steps_blob FROM trajectory "
            "WHERE attempt_id = :id",
            {"id": attempt_id},
            f"no trajectory for attempt {attempt_id}",
        )
        steps_raw = await from_bytea(blobs, bytes(row[3]))
        attempts = await _attempts(session_factory, "a.id = :id", {"id": attempt_id})
        return TrajectoryResponse(
            attempt_id=attempt_id,
            provenance=row[0],
            model_id=row[1],
            n_steps=row[2],
            steps=json.loads(steps_raw),
            verdict=attempts[0].verdict if attempts else None,
        )

    # --- base environments and blobs ------------------------------------------------------------

    @app.get("/v1/base-envs", response_model=list[BaseEnvBody])
    async def list_base_envs() -> list[BaseEnvBody]:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT digest, recipe, toolchain_rev, mathlib_rev, curated FROM base_env "
                        "ORDER BY curated DESC, created_at DESC"
                    )
                )
            ).all()
        return [
            BaseEnvBody(
                digest=bytes(r[0]).hex(),
                recipe=r[1],
                toolchain_rev=r[2],
                mathlib_rev=r[3],
                curated=r[4],
            )
            for r in rows
        ]

    @app.post("/v1/base-envs", response_model=BaseEnvBody, status_code=201)
    async def register_base_env(req: RegisterBaseEnvRequest) -> BaseEnvBody:
        """Spec §6.1: "Register a project prelude; returns `digest`".

        The digest is content-addressed over the recipe, so registering the same prelude twice is
        the same environment rather than two -- which matters directly for the warm-worker pool,
        where a duplicate digest would mean a second full memory slot for an identical environment
        (spec §6.2's header fragmentation).

        `curated` is false and cannot be set here: spec's curated set is what runs are *steered*
        toward, and a caller that could mark its own prelude curated could opt into the hot pool.
        """
        recipe = {"imports": req.imports}
        digest = hashlib.sha256(
            json.dumps(
                {
                    "recipe": recipe,
                    "toolchain_rev": req.toolchain_rev,
                    "mathlib_rev": req.mathlib_rev,
                },
                sort_keys=True,
            ).encode()
        ).digest()
        async with session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO base_env (digest, recipe, toolchain_rev, mathlib_rev, curated) "
                    "VALUES (:d, CAST(:recipe AS jsonb), :toolchain, :mathlib, false) "
                    "ON CONFLICT (digest) DO NOTHING"
                ),
                {
                    "d": digest,
                    "recipe": json.dumps(recipe, sort_keys=True),
                    "toolchain": req.toolchain_rev,
                    "mathlib": req.mathlib_rev,
                },
            )
            await session.commit()
        return BaseEnvBody(
            digest=digest.hex(),
            recipe=recipe,
            toolchain_rev=req.toolchain_rev,
            mathlib_rev=req.mathlib_rev,
            curated=False,
        )

    @app.get("/v1/blobs/{sha256}")
    async def get_blob(sha256: str) -> Response:
        """Spec §6.1 marks this "Tenant-scoped". `blob.tenant_id` carries the scope and a NULL
        means shared, but the *check* needs an authenticated caller, and multi-tenancy is post-MVP
        (spec §9: row-level security on `tenant_id`). Rather than pretend, this serves only blobs
        whose `tenant_id` is NULL -- the shared ones -- so a tenant-owned blob is never handed out
        by an endpoint that cannot yet tell who is asking.
        """
        try:
            digest = bytes.fromhex(sha256)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="not a hex digest") from exc
        row = await _one(
            session_factory,
            "SELECT media_type, tenant_id FROM blob WHERE sha256 = :d",
            {"d": digest},
            f"no blob {sha256}",
        )
        if row[1] is not None:
            raise HTTPException(
                status_code=403,
                detail="this blob is tenant-scoped and this deployment cannot authenticate a "
                "tenant yet (multi-tenancy is post-MVP)",
            )
        return Response(content=await blobs.get(digest), media_type=row[0])

    # --- operational ----------------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: is this process running. Deliberately touches nothing else -- a liveness probe
        that fails when the database is briefly unreachable gets the process killed for someone
        else's outage."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Readiness: can this process actually serve. Touches the database, because an API that
        cannot reach Postgres can answer nothing useful and should be taken out of rotation."""
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # any failure at all means not ready
            raise HTTPException(status_code=503, detail=f"database unreachable: {exc}") from exc
        return {"status": "ready"}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> str:
        async with session_factory() as session:
            obligations = (
                await session.execute(
                    text("SELECT status::text, count(*) FROM obligation GROUP BY status")
                )
            ).all()
            attempts = (
                await session.execute(
                    text("SELECT status::text, count(*) FROM attempt GROUP BY status")
                )
            ).all()
            runs = (
                await session.execute(text("SELECT status, count(*) FROM run GROUP BY status"))
            ).all()
        lines = [
            "# HELP lean_agent_obligations Obligations by status.",
            "# TYPE lean_agent_obligations gauge",
            *(f'lean_agent_obligations{{status="{s}"}} {c}' for s, c in obligations),
            "# HELP lean_agent_attempts Attempts by status.",
            "# TYPE lean_agent_attempts gauge",
            *(f'lean_agent_attempts{{status="{s}"}} {c}' for s, c in attempts),
            "# HELP lean_agent_runs Runs by status.",
            "# TYPE lean_agent_runs gauge",
            *(f'lean_agent_runs{{status="{s}"}} {c}' for s, c in runs),
        ]
        return "\n".join(lines) + "\n"

    return app


async def _attempts(
    sessions: async_sessionmaker[AsyncSession], where: str, params: dict[str, Any]
) -> list[AttemptSummary]:
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT a.id, a.obligation_id, a.status::text, a.policy_id, a.started_at, "
                    "a.finished_at, a.tokens_in, a.tokens_out, a.kernel_ms, a.wallclock_ms, "
                    "v.kind::text, v.link_ok, v.replay_ok, v.axiom_audit_ok, v.axioms, "
                    "v.elapsed_ms, v.cache_hit "
                    "FROM attempt a LEFT JOIN verdict v ON v.attempt_id = a.id "
                    f"WHERE {where} ORDER BY a.started_at DESC"
                ),
                params,
            )
        ).all()
    return [
        AttemptSummary(
            id=r[0],
            obligation_id=r[1],
            status=r[2],
            policy_id=r[3],
            started_at=r[4].isoformat(),
            finished_at=r[5].isoformat() if r[5] is not None else None,
            tokens_in=r[6],
            tokens_out=r[7],
            kernel_ms=r[8],
            wallclock_ms=r[9],
            verdict=(
                VerdictBody(
                    kind=r[10],
                    link_ok=r[11],
                    replay_ok=r[12],
                    axiom_audit_ok=r[13],
                    axioms=list(r[14] or []),
                    elapsed_ms=r[15],
                    cache_hit=r[16],
                )
                if r[10] is not None
                else None
            ),
        )
        for r in rows
    ]


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, sort_keys=True)}\n\n"
