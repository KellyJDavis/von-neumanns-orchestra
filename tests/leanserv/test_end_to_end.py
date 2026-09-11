"""M2.7 exit criterion, and the first time the whole Phase 2 pipeline runs end to end.

Submission -> ingestion -> **materialization** -> claim -> null agent -> link/replay/audit ->
`mark_proved`, against a real kernel and a real PostgreSQL, with zero model calls.

Materialization is what makes this possible at all rather than merely tidier. Before it,
`obligation.sealed_olean_sha` is NULL and `mark_proved` compares the observed digest against NULL,
which is never true -- so no obligation ingested by M2.6 could reach `proved` no matter how
correct its proof was. `test_an_unmaterialized_obligation_cannot_be_proved` pins that down, and it
is the reason this test file exists here rather than as a nicer version of M2.5's.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lean_agent_api.ingestion import Ingestor, Submission
from lean_agent_api.materialize import (
    AssembledFile,
    BundleMaterializer,
    FileMaterializer,
    MaterializationError,
)
from lean_agent_core.actions import Budget, ObligationContext
from lean_agent_core.blobs import LocalBlobStore
from lean_agent_core.enums import VerdictKind
from lean_agent_core.executor import PolicyExecutor, TrajectoryWriter
from lean_agent_core.protocols import (
    CheckOutcome,
    DecomposedLemma,
    DecomposeOutcome,
    LinkOutcome,
    SealedGoal,
    SealGoalRequest,
    SealOutcome,
)
from lean_agent_core.scheduler import ClaimedAttempt, claim_attempt
from lean_agent_core.state import ObligationOutcome, ObligationStateMachine
from lean_agent_policies.symbolic import SymbolicPortfolio
from lean_agent_serv.api import create_app
from lean_agent_serv.cache import VerificationCacheStore
from lean_agent_serv.pool import LeanReplPool, PoolConfig
from lean_agent_serv.verdicts import VerdictWriter
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class LeanServiceOverTestClient:
    """A `LeanService` over the real leanserv app (see M2.5's test for why it lives in a test)."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        response = self._client.post(path, json=payload)
        response.raise_for_status()
        return dict(response.json())

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
        data = self._post("/v1/check", payload)
        return CheckOutcome(
            ok=data["ok"],
            diagnostics=tuple(data["diagnostics"]),
            cache_hit=data["cache_hit"],
            elapsed_ms=data["elapsed_ms"],
            # Carried since M3.9. Without it every check reads as having an empty axiom cone, so
            # the executor's `sorryAx` screen (M2.10) silently passed everything in this suite --
            # harmless for the portfolio's tactics, and exactly wrong for a model's sample, whose
            # first code block is a `sorry` sketch.
            axioms=tuple(data.get("axioms", ())),
        )

    async def seal(
        self,
        *,
        base_env_digest: str,
        goals: Sequence[SealGoalRequest],
        timeout_ms: int | None = None,
    ) -> SealOutcome:
        data = self._post(
            "/v1/seal",
            {
                "base_env_digest": base_env_digest,
                "goals": [
                    {"name": g.name, "statement": g.statement, "level_params": list(g.level_params)}
                    for g in goals
                ],
            },
        )
        return SealOutcome(
            ok=data["ok"],
            goals=tuple(
                SealedGoal(
                    decl_name=g["decl_name"],
                    goal_src=g["goal_src"],
                    goal_digest=g["goal_digest"],
                    level_params=tuple(g["level_params"]),
                    diagnostics=tuple(g["diagnostics"]),
                    ok=g["ok"],
                )
                for g in data["goals"]
            ),
            bundle_source=data["bundle_source"],
            bundle_digest=data["bundle_digest"],
        )

    async def decompose(
        self, *, base_env_digest: str, development: str, timeout_ms: int | None = None
    ) -> DecomposeOutcome:
        data = self._post(
            "/v1/decompose", {"base_env_digest": base_env_digest, "development": development}
        )
        return DecomposeOutcome(
            ok=data["ok"],
            lemmas=tuple(
                DecomposedLemma(
                    name=lemma["name"],
                    statement=lemma["statement"],
                    level_params=tuple(lemma["level_params"]),
                    round_trips=lemma["round_trips"],
                    diagnostics=tuple(lemma["diagnostics"]),
                )
                for lemma in data["lemmas"]
            ),
            reassembly=data["reassembly"],
            diagnostics=tuple(data["diagnostics"]),
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
        data = self._post(
            "/v1/link",
            {
                "attempt_id": str(attempt_id),
                "obligation_id": str(obligation_id),
                "base_env_digest": base_env_digest,
                "bundle_sha": bundle_sha,
                "goal": goal,
                "entry": entry,
                "development": development,
            },
        )
        return LinkOutcome(
            kind=VerdictKind(data["kind"]),
            link_ok=data["link_ok"],
            replay_ok=data["replay_ok"],
            axiom_audit_ok=data["axiom_audit_ok"],
            axioms=tuple(data["axioms"]),
            diagnostics=tuple(data["diagnostics"]),
            elapsed_ms=data["elapsed_ms"],
        )


@pytest.fixture
def bundle_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fresh, empty bundle root -- the directory materialization writes into and every worker
    imports from. Empty at the start deliberately: nothing here is pre-built, so a bundle that
    exists at link time can only have got there through `BundleMaterializer`."""
    return tmp_path_factory.mktemp("e2e_bundles")


@pytest.fixture
def base_env(admin_engine: Engine) -> Iterator[str]:
    digest = f"e2e-{uuid.uuid4()}".encode()
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
    app = create_app(
        pool,
        VerificationCacheStore(sessionmaker, blobs),
        VerdictWriter(sessionmaker, blobs),
        sessionmaker,
    )
    with TestClient(app) as client:
        yield client
    asyncio.run(engine.dispose())


def _cleanup(admin_engine: Engine, run_id: uuid.UUID) -> None:
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": run_id})
        conn.commit()


async def _drive(
    *,
    sessions: async_sessionmaker[AsyncSession],
    leanserv: TestClient,
    blobs: LocalBlobStore,
    bundle_root: Path,
    lake_project_dir: Path,
    submission: Submission,
    materialize: bool,
) -> tuple[uuid.UUID, list[uuid.UUID], ObligationOutcome | None]:
    """The whole pipeline, in the order a deployment runs it.

    `materialize=False` is the counterfactual: everything else identical, the compile step skipped.
    """
    lean = LeanServiceOverTestClient(leanserv)

    result = await Ingestor(session_factory=sessions, lean=lean, blobs=blobs).ingest(submission)
    if materialize:
        await BundleMaterializer(
            session_factory=sessions,
            blobs=blobs,
            bundle_root=bundle_root,
            lake_project_dir=lake_project_dir,
        ).materialize_run(result.run_id)

    policy = SymbolicPortfolio(tactics=("rfl", "decide", "simp"))
    outcome: ObligationOutcome | None = None
    claimed = await claim_attempt(
        sessions,
        worker_id="e2e",
        policy_id=policy.id,
        policy_config_hash=policy.config_hash,
    )
    if claimed is not None:
        ctx, budget = await _context_for(sessions, claimed)
        executor = PolicyExecutor(
            policy=policy,
            lean=lean,
            trajectories=TrajectoryWriter(sessions, blobs),
            context_loader=lambda o, r: _context_for(sessions, claimed),
        )
        attempt_result = await executor.execute(
            attempt_id=claimed.attempt_id, ctx=ctx, budget=budget
        )
        outcome = attempt_result.outcome
        if outcome is ObligationOutcome.PROVED:
            await ObligationStateMachine(sessions).mark_proved(
                claimed.obligation_id, claimed.attempt_id
            )
    return result.run_id, result.root_obligations, outcome


async def _context_for(
    sessions: async_sessionmaker[AsyncSession], claimed: ClaimedAttempt
) -> tuple[ObligationContext, Budget]:
    """Band 1 for the claimed obligation, read from the row ingestion wrote."""
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT base_env_digest, bundle_sha, decl_name, goal_src, "
                    "budget_attempts - spent_attempts FROM obligation WHERE id = :id"
                ),
                {"id": claimed.obligation_id},
            )
        ).one()
    base_env, bundle_sha, decl_name, goal_src, attempts_left = row
    return (
        ObligationContext(
            obligation_id=claimed.obligation_id,
            run_id=claimed.run_id,
            base_env_digest=bytes(base_env).hex(),
            bundle_sha=bytes(bundle_sha).hex(),
            goal_decl=decl_name,
            goal_src=goal_src,
            entry=decl_name.replace("LeanAgent.Goals.G_", "LeanAgent.Sol.sol_", 1),
        ),
        Budget(attempts_remaining=int(attempts_left)),
    )


def _run(
    app_async_database_url: str,
    tmp_path: Path,
    leanserv: TestClient,
    bundle_root: Path,
    lake_project_dir: Path,
    submission: Submission,
    *,
    materialize: bool = True,
) -> tuple[uuid.UUID, list[uuid.UUID], ObligationOutcome | None]:
    async def main() -> tuple[uuid.UUID, list[uuid.UUID], ObligationOutcome | None]:
        engine = create_async_engine(app_async_database_url)
        try:
            return await _drive(
                sessions=async_sessionmaker(engine, expire_on_commit=False),
                leanserv=leanserv,
                blobs=LocalBlobStore(tmp_path / "app_blobs"),
                bundle_root=bundle_root,
                lake_project_dir=lake_project_dir,
                submission=submission,
                materialize=materialize,
            )
        finally:
            await engine.dispose()

    return asyncio.run(main())


def test_a_submission_becomes_a_proved_obligation(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    bundle_root: Path,
    lake_project_dir: Path,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """Phase 2, complete: a statement goes in and comes out `proved` through the real acceptance
    path, with zero model calls.

    Every step is the real one -- ingestion sealed the goal in a warm worker, materialization
    compiled the bundle with `lake env lean`, the scheduler claimed it, `SymbolicPortfolio`
    proposed tactics, `/v1/link` type-checked the winner against the sealed constant in the kernel,
    replayed it and audited its axiom cone, and `mark_proved` re-checked the §1.1 predicate against
    the verdict before letting the status move.
    """
    submission = Submission(
        base_env_digest=base_env, tenant_id=uuid.uuid4(), statement="(2 : Nat) + 2 = 4"
    )
    run_id, roots, outcome = _run(
        app_async_database_url, tmp_path, leanserv, bundle_root, lake_project_dir, submission
    )
    try:
        assert outcome is ObligationOutcome.PROVED
        (obligation,) = roots
        with admin_engine.connect() as conn:
            status, sealed_olean, bundle_sha = conn.execute(
                text(
                    "SELECT status::text, sealed_olean_sha, bundle_sha FROM obligation "
                    "WHERE id = :id"
                ),
                {"id": obligation},
            ).one()
            verdict = conn.execute(
                text(
                    "SELECT kind::text, link_ok, replay_ok, axiom_audit_ok, "
                    "sealed_olean_sha_observed, proof_blob FROM verdict WHERE obligation_id = :id"
                ),
                {"id": obligation},
            ).one()
        assert status == "proved"
        # Materialization filled it, and the verdict observed the very same digest -- which is the
        # seal-integrity check `mark_proved` requires and the reason this could not pass before.
        assert sealed_olean is not None
        assert verdict[4] == sealed_olean
        assert (verdict[0], verdict[1], verdict[2], verdict[3]) == ("proved", True, True, True)
        # The accepted proof text is on the verdict, which is where §6.3 step 6 will read it from.
        assert verdict[5] is not None
        # And the compiled artifact really is on disk under the bundle root every worker imports.
        olean = bundle_root / "LeanAgent" / "Goals" / f"Bundle_{bytes(bundle_sha).hex()}.olean"
        assert olean.exists()
    finally:
        _cleanup(admin_engine, run_id)


def test_an_unmaterialized_obligation_cannot_be_proved(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    bundle_root: Path,
    lake_project_dir: Path,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """The counterfactual that gives materialization its meaning.

    Identical submission, identical policy, compile step skipped. The bundle is not on any worker's
    path, so `/v1/link` refuses before a worker is even acquired -- and `sealed_olean_sha` is NULL,
    so `mark_proved` would refuse anyway, since it compares the observed digest against NULL. An
    obligation that is merely *recorded* is not provable, however correct its proof.
    """
    submission = Submission(
        base_env_digest=base_env, tenant_id=uuid.uuid4(), statement="(2 : Nat) + 2 = 4"
    )
    run_ids: list[uuid.UUID] = []

    async def main() -> None:
        engine = create_async_engine(app_async_database_url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            blobs = LocalBlobStore(tmp_path / "app_blobs")
            lean = LeanServiceOverTestClient(leanserv)
            # Ingest first and keep the run id, so the run is cleaned up even though the rest of
            # the pipeline is about to fail -- otherwise the base_env fixture's own teardown hits
            # a foreign-key violation and the real assertion is buried under it.
            result = await Ingestor(session_factory=sessions, lean=lean, blobs=blobs).ingest(
                submission
            )
            run_ids.append(result.run_id)

            with admin_engine.connect() as conn:
                assert (
                    conn.execute(
                        text("SELECT sealed_olean_sha FROM obligation WHERE run_id = :r"),
                        {"r": result.run_id},
                    ).scalar_one()
                    is None
                )

            policy = SymbolicPortfolio(tactics=("decide",))
            claimed = await claim_attempt(
                sessions,
                worker_id="e2e",
                policy_id=policy.id,
                policy_config_hash=policy.config_hash,
            )
            assert claimed is not None
            ctx, budget = await _context_for(sessions, claimed)
            executor = PolicyExecutor(
                policy=policy,
                lean=lean,
                trajectories=TrajectoryWriter(sessions, blobs),
                context_loader=lambda o, r: _context_for(sessions, claimed),
            )
            with pytest.raises(Exception, match="404|not materialized"):
                await executor.execute(attempt_id=claimed.attempt_id, ctx=ctx, budget=budget)
        finally:
            await engine.dispose()

    try:
        asyncio.run(main())
    finally:
        for run_id in run_ids:
            _cleanup(admin_engine, run_id)


def test_materialization_is_idempotent_and_write_once(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    bundle_root: Path,
    lake_project_dir: Path,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """There is no natural single moment to materialize -- a restart, a retry, or a second
    obligation against the same bundle all reasonably lead here -- so running it twice must be
    safe, and must not be able to *change* a digest an obligation is already judged against.
    """
    submission = Submission(base_env_digest=base_env, tenant_id=uuid.uuid4(), statement="True")

    async def main() -> tuple[uuid.UUID, int, int, bytes]:
        engine = create_async_engine(app_async_database_url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            blobs = LocalBlobStore(tmp_path / "app_blobs")
            result = await Ingestor(
                session_factory=sessions,
                lean=LeanServiceOverTestClient(leanserv),
                blobs=blobs,
            ).ingest(submission)
            materializer = BundleMaterializer(
                session_factory=sessions,
                blobs=blobs,
                bundle_root=bundle_root,
                lake_project_dir=lake_project_dir,
            )
            assert result.bundle_sha is not None
            first = await materializer.materialize(result.bundle_sha)
            second = await materializer.materialize(result.bundle_sha)
            assert first.olean_sha == second.olean_sha
            return (
                result.run_id,
                first.obligations_stamped,
                second.obligations_stamped,
                first.olean_sha,
            )
        finally:
            await engine.dispose()

    run_id, first_stamped, second_stamped, olean_sha = asyncio.run(main())
    try:
        assert first_stamped == 1
        # Nothing left to fill: the second pass compiles the same content-addressed source to the
        # same digest and stamps nobody.
        assert second_stamped == 0
        with admin_engine.connect() as conn:
            stored = conn.execute(
                text("SELECT sealed_olean_sha FROM obligation WHERE run_id = :r"), {"r": run_id}
            ).scalar_one()
            # Write-once has teeth: a *different* digest for an already-materialized bundle is
            # refused outright rather than silently ignored, because the two facts disagreeing
            # would mean the build is not reproducible or the bundle root was tampered with.
            with pytest.raises(Exception, match="already materialized with a different"):
                conn.execute(
                    text("SELECT materialize_bundle(:b, :o)"),
                    {
                        "b": conn.execute(
                            text("SELECT bundle_sha FROM obligation WHERE run_id = :r"),
                            {"r": run_id},
                        ).scalar_one(),
                        "o": b"a completely different digest",
                    },
                )
        assert bytes(stored) == olean_sha
    finally:
        _cleanup(admin_engine, run_id)


def _prove_all(
    sessions: async_sessionmaker[AsyncSession], leanserv: TestClient
) -> Callable[[], Awaitable[int]]:
    """Claim and prove every schedulable obligation until the queue is empty. Returns how many
    were proved -- the tests assert on that rather than on a fixed loop count."""

    async def run() -> int:
        lean = LeanServiceOverTestClient(leanserv)
        policy = SymbolicPortfolio(tactics=("rfl", "decide", "simp"))
        proved = 0
        for _ in range(12):
            claimed = await claim_attempt(
                sessions,
                worker_id="assembly",
                policy_id=policy.id,
                policy_config_hash=policy.config_hash,
            )
            if claimed is None:
                break
            ctx, budget = await _context_for(sessions, claimed)
            executor = PolicyExecutor(
                policy=policy,
                lean=lean,
                trajectories=TrajectoryWriter(sessions, LocalBlobStore(Path("/tmp"))),
                context_loader=lambda o, r, c=claimed: _context_for(sessions, c),  # type: ignore[misc]
            )
            result = await executor.execute(attempt_id=claimed.attempt_id, ctx=ctx, budget=budget)
            if result.outcome is ObligationOutcome.PROVED:
                await ObligationStateMachine(sessions).mark_proved(
                    claimed.obligation_id, claimed.attempt_id
                )
                proved += 1
            else:
                await ObligationStateMachine(sessions).retryable_failure(claimed.obligation_id)
        return proved

    return run


def test_a_proved_file_assembles_into_a_standalone_lean_file(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    bundle_root: Path,
    lake_project_dir: Path,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """Spec §6.3 step 6, end to end: a submitted file with holes comes back with them filled.

    The two checks are what the step is for. Elaborating proves the pieces compile together;
    linking each declaration against its *sealed* constant proves they prove what was asked --
    "without the link the materialized file could compile cleanly with a drifted statement".
    """
    submission = Submission(
        base_env_digest=base_env,
        tenant_id=uuid.uuid4(),
        source=(
            "theorem both : (1 : Nat) + 1 = 2 ∧ (2 : Nat) + 2 = 4 := by\n"
            "  constructor\n  · sorry\n  · sorry"
        ),
    )

    async def main() -> tuple[uuid.UUID, int, AssembledFile]:
        engine = create_async_engine(app_async_database_url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            blobs = LocalBlobStore(tmp_path / "app_blobs")
            lean = LeanServiceOverTestClient(leanserv)
            result = await Ingestor(session_factory=sessions, lean=lean, blobs=blobs).ingest(
                submission
            )
            await BundleMaterializer(
                session_factory=sessions,
                blobs=blobs,
                bundle_root=bundle_root,
                lake_project_dir=lake_project_dir,
            ).materialize_run(result.run_id)
            proved = await _prove_all(sessions, leanserv)()
            assembled = await FileMaterializer(
                session_factory=sessions, blobs=blobs, lean=lean
            ).assemble(result.run_id)
            return result.run_id, proved, assembled
        finally:
            await engine.dispose()

    run_id, proved, assembled = asyncio.run(main())
    try:
        assert proved == 2
        assert assembled.holes == 2
        assert assembled.unfilled == ()
        assert assembled.elaborates is True
        assert assembled.links is True
        assert assembled.complete is True

        # Standalone: the sealed goals are inlined, not imported from a per-run generated module a
        # person taking this file away would not have.
        assert "import LeanAgent.Goals.Bundle_" not in assembled.source
        assert "def G_1 : Sort _ :=" in assembled.source
        # The original theorem is back, with its holes filled by references to the accepted proofs.
        assert "theorem both :" in assembled.source
        assert "abbrev sorry_1 := @LeanAgent.Sol.sol_1" in assembled.source
        assert "@sorry_1" in assembled.source

        # The strongest available check on "standalone", and stronger than `elaborates`: compile
        # the emitted artifact itself, as a file, against nothing but the toolchain -- no bundle
        # root on `LEAN_PATH`, no warm base environment. If it needed the generated bundle module
        # it would fail here.
        artifact = tmp_path / "Materialized.lean"
        artifact.write_text(assembled.source)
        compiled = subprocess.run(
            ["lake", "env", "lean", f"--root={tmp_path}", str(artifact)],
            cwd=lake_project_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    finally:
        _cleanup(admin_engine, run_id)


def test_an_unproved_hole_is_reported_rather_than_hidden(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    bundle_root: Path,
    lake_project_dir: Path,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """A partially-proved run still produces an artifact, and it says which holes are open.

    Emitting the file anyway is deliberate: a partial result is the useful thing to hand back, and
    `complete` is what says whether it is finished. Silently omitting an unproved declaration would
    produce a file that fails to elaborate for a reason its reader cannot see.
    """
    submission = Submission(
        base_env_digest=base_env,
        tenant_id=uuid.uuid4(),
        source="theorem hard : ∀ n : Nat, 2 ^ n ≥ n + 1 := by sorry",
    )

    async def main() -> tuple[uuid.UUID, AssembledFile]:
        engine = create_async_engine(app_async_database_url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            blobs = LocalBlobStore(tmp_path / "app_blobs")
            lean = LeanServiceOverTestClient(leanserv)
            result = await Ingestor(session_factory=sessions, lean=lean, blobs=blobs).ingest(
                submission
            )
            await BundleMaterializer(
                session_factory=sessions,
                blobs=blobs,
                bundle_root=bundle_root,
                lake_project_dir=lake_project_dir,
            ).materialize_run(result.run_id)
            # The portfolio cannot close it; do not even try, so the test stays fast and its point
            # stays "an unfilled hole is reported", not "these tactics fail".
            assembled = await FileMaterializer(
                session_factory=sessions, blobs=blobs, lean=lean
            ).assemble(result.run_id)
            return result.run_id, assembled
        finally:
            await engine.dispose()

    run_id, assembled = asyncio.run(main())
    try:
        assert assembled.unfilled == ("sorry_1",)
        assert assembled.complete is False
        # Named in the file itself, not only in the report.
        assert "UNPROVED" in assembled.source
    finally:
        _cleanup(admin_engine, run_id)


def test_a_bare_statement_has_no_file_to_assemble(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """`run.reassembly_blob` is NULL for a statement submission, and that is not a missing value to
    work around -- there was never a file with holes in it."""
    submission = Submission(
        base_env_digest=base_env, tenant_id=uuid.uuid4(), statement="(2 : Nat) + 2 = 4"
    )

    async def main() -> uuid.UUID:
        engine = create_async_engine(app_async_database_url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            blobs = LocalBlobStore(tmp_path / "app_blobs")
            lean = LeanServiceOverTestClient(leanserv)
            result = await Ingestor(session_factory=sessions, lean=lean, blobs=blobs).ingest(
                submission
            )
            with pytest.raises(MaterializationError, match="no reassembly"):
                await FileMaterializer(session_factory=sessions, blobs=blobs, lean=lean).assemble(
                    result.run_id
                )
            return result.run_id
        finally:
            await engine.dispose()

    _cleanup(admin_engine, asyncio.run(main()))
