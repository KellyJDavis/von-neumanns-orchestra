"""M2.5 exit criterion: the null agent proving a real Lean goal, end to end.

`SymbolicPortfolio` -> `PolicyExecutor` -> real `/v1/link` -> real `leankernel serve` -> real
kernel, against a real materialized bundle and a real PostgreSQL. Zero model calls, which is
Phase 2's own headline requirement and is checkable here rather than asserted: the policy's
`roles` is empty and the executor refuses `RequestCompletion` outright.

The `LeanService` implementation is written here, over the real FastAPI app via `TestClient`. That
is deliberate for this milestone: the protocol needs one real implementation to prove its shape is
right, and the *deployed* client is an httpx one that belongs with M2.9 (the CLI as an httpx client
of the public API). Writing it here exercises the genuine `/v1/link` path -- real worker, real
kernel -- without prematurely committing to an HTTP client design.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import MaterializedBundle
from fastapi.testclient import TestClient
from lean_agent_core.actions import Budget, ObligationContext
from lean_agent_core.blobs import LocalBlobStore
from lean_agent_core.enums import ProvenanceClass, VerdictKind
from lean_agent_core.executor import PolicyExecutor, TrajectoryWriter
from lean_agent_core.protocols import CheckOutcome, LinkOutcome
from lean_agent_core.state import ObligationOutcome
from lean_agent_policies.symbolic import SymbolicPortfolio
from lean_agent_serv.api import create_app
from lean_agent_serv.cache import VerificationCacheStore
from lean_agent_serv.pool import LeanReplPool, PoolConfig
from lean_agent_serv.verdicts import VerdictWriter
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class LeanServiceOverTestClient:
    """A `LeanService` over the real leanserv app. See the module docstring on why it lives in a
    test rather than in `packages/`."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    async def check(
        self,
        *,
        base_env_digest: str,
        body: str,
        bundle_sha: str | None = None,
        timeout_ms: int | None = None,
    ) -> CheckOutcome:
        payload: dict[str, object] = {"base_env_digest": base_env_digest, "body": body}
        if bundle_sha is not None:
            payload["bundle_sha"] = bundle_sha
        if timeout_ms is not None:
            payload["timeout_ms"] = timeout_ms
        response = self._client.post("/v1/check", json=payload)
        response.raise_for_status()
        body_json = response.json()
        return CheckOutcome(
            ok=body_json["ok"],
            diagnostics=tuple(body_json["diagnostics"]),
            cache_hit=body_json["cache_hit"],
            elapsed_ms=body_json["elapsed_ms"],
        )

    async def link(
        self,
        *,
        attempt_id: uuid.UUID,
        obligation_id: uuid.UUID,
        base_env_digest: str,
        bundle_sha: str,
        goal: str,
        entry: str,
        development: str,
        timeout_ms: int | None = None,
    ) -> LinkOutcome:
        payload: dict[str, object] = {
            "attempt_id": str(attempt_id),
            "obligation_id": str(obligation_id),
            "base_env_digest": base_env_digest,
            "bundle_sha": bundle_sha,
            "goal": goal,
            "entry": entry,
            "development": development,
        }
        if timeout_ms is not None:
            payload["timeout_ms"] = timeout_ms
        response = self._client.post("/v1/link", json=payload)
        response.raise_for_status()
        body = response.json()
        return LinkOutcome(
            kind=VerdictKind(body["kind"]),
            link_ok=body["link_ok"],
            replay_ok=body["replay_ok"],
            axiom_audit_ok=body["axiom_audit_ok"],
            axioms=tuple(body["axioms"]),
            diagnostics=tuple(body["diagnostics"]),
            elapsed_ms=body["elapsed_ms"],
        )


class Fixture:
    def __init__(self, engine: Engine, run_id: uuid.UUID, base_env_digest: bytes) -> None:
        self._engine = engine
        self.run_id = run_id
        self.base_env_digest = base_env_digest

    def obligation_and_attempt(
        self, decl_name: str, sealed_olean_sha: bytes
    ) -> tuple[uuid.UUID, uuid.UUID]:
        obligation_id, attempt_id = uuid.uuid4(), uuid.uuid4()
        with self._engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO obligation (id, run_id, base_env_digest, goal_digest, "
                    "sealed_olean_sha, goal_src, decl_name, status) VALUES (:id, :run, :base_env, "
                    ":gd, :sealed, 'src', :decl, 'in_progress')"
                ),
                {
                    "id": obligation_id,
                    "run": self.run_id,
                    "base_env": self.base_env_digest,
                    "gd": f"goal-{uuid.uuid4()}".encode(),
                    "sealed": sealed_olean_sha,
                    "decl": decl_name,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO attempt (id, obligation_id, run_id, policy_id, "
                    "policy_config_hash) VALUES (:id, :obl, :run, 'SymbolicPortfolio', :cfg)"
                ),
                {
                    "id": attempt_id,
                    "obl": obligation_id,
                    "run": self.run_id,
                    "cfg": SymbolicPortfolio().config_hash,
                },
            )
            conn.commit()
        return obligation_id, attempt_id

    def trajectory(self, attempt_id: uuid.UUID) -> tuple[object, ...]:
        with self._engine.connect() as conn:
            return tuple(
                conn.execute(
                    text(
                        "SELECT provenance::text, n_steps, model_id FROM trajectory "
                        "WHERE attempt_id = :id"
                    ),
                    {"id": attempt_id},
                ).one()
            )


@pytest.fixture
def fx(admin_engine: Engine, materialized_bundle: MaterializedBundle) -> Iterator[Fixture]:
    base_env_digest = f"null-agent-{uuid.uuid4()}".encode()
    run_id = uuid.uuid4()
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO base_env (digest, recipe, toolchain_rev, mathlib_rev) VALUES "
                "(:digest, '{\"imports\": [\"Init\"]}', 'v4.33.1', 'deadbeef')"
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


def _run_policy(
    app_async_database_url: str,
    tmp_path: Path,
    leanserv: TestClient,
    ctx: ObligationContext,
    attempt_id: uuid.UUID,
    policy: SymbolicPortfolio,
) -> object:
    async def main() -> object:
        engine = create_async_engine(app_async_database_url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)

            async def load(o: uuid.UUID, r: uuid.UUID) -> tuple[ObligationContext, Budget]:
                del o, r
                return ctx, Budget(attempts_remaining=8)

            executor = PolicyExecutor(
                policy=policy,
                lean=LeanServiceOverTestClient(leanserv),
                trajectories=TrajectoryWriter(sessions, LocalBlobStore(tmp_path)),
                context_loader=load,
            )
            return await executor.execute(
                attempt_id=attempt_id, ctx=ctx, budget=Budget(attempts_remaining=8)
            )
        finally:
            await engine.dispose()

    return asyncio.run(main())


def test_the_null_agent_proves_a_real_goal_with_no_model_calls(
    fx: Fixture,
    leanserv: TestClient,
    materialized_bundle: MaterializedBundle,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """The whole point of Phase 2, running for real.

    `G_add_zero` is `∀ n : Nat, n + 0 = n` in a genuinely compiled bundle. The policy never states
    that -- it emits `def sol : LeanAgent.Goals.G_add_zero := by unfold ...; <tactic>` -- and the
    kernel accepts the result against the sealed constant. `rfl` fails, `decide` fails, `simp`
    closes it, and the trajectory records which one did.
    """
    obligation, attempt = fx.obligation_and_attempt(
        "LeanAgent.Goals.G_add_zero", materialized_bundle.olean_digest
    )
    ctx = ObligationContext(
        obligation_id=obligation,
        run_id=fx.run_id,
        base_env_digest=fx.base_env_digest.hex(),
        bundle_sha=materialized_bundle.sha,
        goal_decl="LeanAgent.Goals.G_add_zero",
        goal_src="∀ n : Nat, n + 0 = n",
        entry="LeanAgent.Sol.sol_add_zero",
    )
    # A trimmed portfolio: the full default would spend real kernel time on a dozen Mathlib
    # tactics that are not even available in an `Init`-only base env. Ordering and stop-on-success
    # are what is under test, not portfolio breadth.
    policy = SymbolicPortfolio(tactics=("rfl", "decide", "simp", "omega"))

    result = _run_policy(app_async_database_url, tmp_path, leanserv, ctx, attempt, policy)

    assert result.outcome is ObligationOutcome.PROVED  # type: ignore[attr-defined]
    assert result.spend.kernel_ms > 0  # type: ignore[attr-defined]

    provenance, n_steps, model_id = fx.trajectory(attempt)
    # Zero completions, so §7.1's rule records this as unencumbered training data.
    assert provenance == ProvenanceClass.SYMBOLIC.value
    assert model_id is None
    # Three steps, not four: `rfl` and `decide` failed their screening check, `simp` passed and
    # was linked, and `omega` was never reached because the attempt's one verdict is spent.
    assert n_steps == 3


def test_the_portfolio_stops_at_the_first_success(
    fx: Fixture,
    leanserv: TestClient,
    materialized_bundle: MaterializedBundle,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """Stopping is the entire cost model of a timed portfolio. A goal the first tactic closes must
    cost exactly one kernel check, not the whole list."""
    obligation, attempt = fx.obligation_and_attempt(
        "LeanAgent.Goals.G_poly", materialized_bundle.olean_digest
    )
    ctx = ObligationContext(
        obligation_id=obligation,
        run_id=fx.run_id,
        base_env_digest=fx.base_env_digest.hex(),
        bundle_sha=materialized_bundle.sha,
        goal_decl="LeanAgent.Goals.G_poly",
        goal_src="PUnit",
        entry="LeanAgent.Sol.sol_poly",
        level_params=("u_1",),
    )
    policy = SymbolicPortfolio(tactics=("exact PUnit.unit", "decide", "simp", "omega"))

    result = _run_policy(app_async_database_url, tmp_path, leanserv, ctx, attempt, policy)

    assert result.outcome is ObligationOutcome.PROVED  # type: ignore[attr-defined]
    assert fx.trajectory(attempt)[1] == 1


def test_an_unprovable_goal_exhausts_the_portfolio_and_reports_a_retryable_failure(
    fx: Fixture,
    leanserv: TestClient,
    materialized_bundle: MaterializedBundle,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """Exhausting the portfolio is an ordinary failed attempt, not an error: the obligation stays
    provable by something else, and the budget is what eventually stops asking."""
    obligation, attempt = fx.obligation_and_attempt(
        "LeanAgent.Goals.G_add_zero", materialized_bundle.olean_digest
    )
    ctx = ObligationContext(
        obligation_id=obligation,
        run_id=fx.run_id,
        base_env_digest=fx.base_env_digest.hex(),
        bundle_sha=materialized_bundle.sha,
        goal_decl="LeanAgent.Goals.G_add_zero",
        goal_src="∀ n : Nat, n + 0 = n",
        entry="LeanAgent.Sol.sol_add_zero",
    )
    policy = SymbolicPortfolio(tactics=("rfl", "decide"))

    result = _run_policy(app_async_database_url, tmp_path, leanserv, ctx, attempt, policy)

    assert result.outcome is ObligationOutcome.RETRYABLE_FAILURE  # type: ignore[attr-defined]
    provenance, n_steps, _ = fx.trajectory(attempt)
    # Still a symbolic trajectory: a failed portfolio run is training data too, and arguably the
    # more interesting kind.
    assert provenance == ProvenanceClass.SYMBOLIC.value
    assert n_steps == 2
