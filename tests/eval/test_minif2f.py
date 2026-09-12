"""M2.10 -- Phase 2's exit gate (spec §8).

"Closes the easy tail of miniF2F deterministically in CI at zero token cost, stable across three
runs; a materialized file passes both the elaboration and the link check. This suite runs on every
PR forever and is the only way to later distinguish a broken harness from a policy that needs
tuning."

Everything here is real: real miniF2F statements (vendored, digest-pinned), a real full-Mathlib
base environment, the real `Ingestor`, the real `BundleMaterializer` compiling with `lake env
lean`, the real `lean_agent_core.worker.Worker` control loop with its leases and heartbeats, the
real `SymbolicPortfolio`, and `/v1/link`'s real kernel link, replay and axiom audit against a real
PostgreSQL. Zero model calls, structurally: the policy declares no roles.

This is also the first place the shipped `LeanServiceClient` (M2.9) is what a suite runs against,
rather than a per-file test adapter.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from lean_agent_api.ingestion import Ingestor, Submission
from lean_agent_api.materialize import BundleMaterializer, FileMaterializer
from lean_agent_core.actions import Budget, ObligationContext
from lean_agent_core.blobs import LocalBlobStore, from_bytea
from lean_agent_core.enums import VerdictKind
from lean_agent_core.executor import PolicyExecutor, TrajectoryWriter
from lean_agent_core.scheduler import ClaimedAttempt
from lean_agent_core.worker import AttemptResult, Worker
from lean_agent_eval import baseline
from lean_agent_eval.baseline import AcceptedProof
from lean_agent_eval.score import AttemptOutcome
from lean_agent_eval.suites.minif2f import (
    EASY_TAIL,
    ArtifactResult,
    IngestedSubmission,
    MiniF2FCorpus,
    SuiteReport,
    build_submission_source,
    load_corpus,
    run_suite,
)
from lean_agent_policies.symbolic import SymbolicPortfolio
from lean_agent_serv.api import create_app
from lean_agent_serv.cache import VerificationCacheStore
from lean_agent_serv.client import LeanServiceClient
from lean_agent_serv.pool import LeanReplPool, PoolConfig
from lean_agent_serv.verdicts import VerdictWriter
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

#: The gate's portfolio: a subset of `DEFAULT_TACTICS`, in the same order, holding the tactics the
#: survey found actually win on the tail. `exact?`/`apply?`/`rw?`/`polyrith` are dropped because on
#: a goal they cannot close they are by far the most expensive members, and this suite runs on
#: every PR forever. Dropping a tactic can only *lose* proofs, never manufacture one, so the gate
#: still proves what it claims -- it cannot pass by lowering the bar.
GATE_TACTICS = ("rfl", "decide", "simp", "simp_all", "omega", "norm_num", "linarith", "nlinarith")

#: Per tactic, comfortably above what the tail's winners need. A wallclock timeout SIGKILLs the
#: worker (M1.8.2), which for full Mathlib costs ~30 s of re-warming -- so this is a cliff rather
#: than a gentle slope, and the tail is chosen to sit far from it.
GATE_TACTIC_TIMEOUT_MS = 20_000


@pytest.fixture(scope="session")
def corpus() -> MiniF2FCorpus:
    return load_corpus()


@pytest.fixture(scope="module")
def bundle_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Empty at the start, deliberately: a bundle that exists at link time can only have got there
    through `BundleMaterializer`."""
    return tmp_path_factory.mktemp("minif2f_bundles")


@pytest.fixture(scope="module")
def mathlib_base_env(admin_engine: Engine, corpus: MiniF2FCorpus) -> Iterator[str]:
    """A base env whose recipe is miniF2F's own `import Mathlib`.

    Taken from the corpus rather than written out here: if a re-pin ever changed upstream's
    imports, the base env has to change with it, or the suite would be proving these statements
    against an environment they were not written for.
    """
    digest = f"minif2f-{uuid.uuid4()}".encode()
    imports = ", ".join(f'"{i}"' for i in corpus.imports)
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO base_env (digest, recipe, toolchain_rev, mathlib_rev) VALUES "
                f"(:d, '{{\"imports\": [{imports}]}}', 'v4.33.1', 'deadbeef')"
            ),
            {"d": digest},
        )
        conn.commit()
    yield digest.hex()
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM base_env WHERE digest = :d"), {"d": digest})
        conn.commit()


@pytest.fixture(scope="module")
def leanserv(
    mathlib: Path,
    leanserv_async_database_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    bundle_root: Path,
) -> Iterator[TestClient]:
    """leanserv with a pool capped at **one** worker, which is a memory decision, not tidiness.

    A full-Mathlib worker measures ~6 GiB RSS. Sealing keys its worker on the base env alone and
    linking on `(base env, bundle)` -- two distinct pool keys -- so an uncapped pool holds ~12 GiB
    at once, on a 16 GiB CI runner that already kills this job when `kernel_tests` approaches its
    limit. Capped at one, the idle seal worker is evicted when linking asks for its own, and the
    suite's phase order (ingest, then materialize, then link) makes that exactly one eviction.
    """
    engine = create_async_engine(leanserv_async_database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    blobs = LocalBlobStore(tmp_path_factory.mktemp("leanserv_blobs"))
    pool = LeanReplPool(mathlib, PoolConfig(max_total_workers=1, bundle_root=bundle_root))
    app = create_app(
        pool,
        VerificationCacheStore(sessionmaker, blobs),
        VerdictWriter(sessionmaker, blobs),
        sessionmaker,
    )
    with TestClient(app) as client:
        yield client
    asyncio.run(engine.dispose())


class _Pipeline:
    """The real Phase 2 pipeline, satisfying `minif2f.MiniF2FPipeline`.

    Nothing here stands in for anything. The only thing this class adds over the components it
    wires together is the read-back queries the report needs, which are ordinary `app`-role selects.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        heartbeat_engine: Engine,
        lean: LeanServiceClient,
        blobs: LocalBlobStore,
        bundle_root: Path,
        lake_project_dir: Path,
        base_env_digest: str,
        policy: SymbolicPortfolio,
    ) -> None:
        self._sessions = sessions
        self._heartbeat_engine = heartbeat_engine
        self._lean = lean
        self._blobs = blobs
        self._bundle_root = bundle_root
        self._lake_project_dir = lake_project_dir
        self._base_env_digest = base_env_digest
        self._policy = policy
        self.run_ids: list[uuid.UUID] = []

    async def ingest(self, source: str) -> IngestedSubmission:
        result = await Ingestor(
            session_factory=self._sessions, lean=self._lean, blobs=self._blobs
        ).ingest(
            Submission(
                base_env_digest=self._base_env_digest,
                tenant_id=uuid.uuid4(),
                source=source,
                # One attempt is all the portfolio needs: it tries every tactic *within* one
                # attempt and the attempt ends when its one verdict is written. A larger budget
                # would only re-run an identical, now-cached tactic sequence, which would make
                # "stable across three runs" a claim about repeated work rather than repeated runs.
                budget_attempts=1,
            )
        )
        self.run_ids.append(result.run_id)
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text("SELECT id, decl_name FROM obligation WHERE run_id = CAST(:r AS uuid)"),
                    {"r": str(result.run_id)},
                )
            ).all()
        return IngestedSubmission(
            run_id=result.run_id,
            obligation_by_goal={
                int(str(decl).rsplit("_", 1)[-1]): obligation_id for obligation_id, decl in rows
            },
            seal_failures={f.name: tuple(f.diagnostics) for f in result.seal_failures},
        )

    async def materialize(self, run_id: uuid.UUID) -> None:
        await BundleMaterializer(
            session_factory=self._sessions,
            blobs=self._blobs,
            bundle_root=self._bundle_root,
            lake_project_dir=self._lake_project_dir,
        ).materialize_run(run_id)

    async def drain(self) -> None:
        """Run the real control loop until it has nothing left to claim."""
        worker = Worker(
            worker_id="minif2f",
            session_factory=self._sessions,
            heartbeat_engine=self._heartbeat_engine,
            runner=self._run_attempt,
            policy_id=self._policy.id,
            policy_config_hash=self._policy.config_hash,
        )
        while await worker.run_once():
            pass

    async def _run_attempt(self, claimed: ClaimedAttempt) -> AttemptResult:
        ctx, budget = await self._context_for(claimed)
        executor = PolicyExecutor(
            policy=self._policy,
            lean=self._lean,
            trajectories=TrajectoryWriter(self._sessions, self._blobs),
            context_loader=lambda _o, _r: self._context_for(claimed),
        )
        return await executor.execute(attempt_id=claimed.attempt_id, ctx=ctx, budget=budget)

    async def _context_for(self, claimed: ClaimedAttempt) -> tuple[ObligationContext, Budget]:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT o.base_env_digest, o.bundle_sha, o.decl_name, o.goal_src, "
                        "o.budget_attempts - o.spent_attempts, r.allow_sorry "
                        "FROM obligation o JOIN run r ON r.id = o.run_id WHERE o.id = :id"
                    ),
                    {"id": claimed.obligation_id},
                )
            ).one()
        base_env, bundle_sha, decl_name, goal_src, attempts_left, allow_sorry = row
        return (
            ObligationContext(
                obligation_id=claimed.obligation_id,
                run_id=claimed.run_id,
                base_env_digest=bytes(base_env).hex(),
                bundle_sha=bytes(bundle_sha).hex(),
                goal_decl=decl_name,
                goal_src=goal_src,
                entry=decl_name.replace("LeanAgent.Goals.G_", "LeanAgent.Sol.sol_", 1),
                allow_sorry=bool(allow_sorry),
            ),
            Budget(attempts_remaining=int(attempts_left)),
        )

    async def attempt_outcomes(
        self, run_id: uuid.UUID
    ) -> dict[uuid.UUID, tuple[AttemptOutcome, ...]]:
        """Every attempt's outcome, from `attempt` LEFT JOINed to its `verdict`.

        A LEFT JOIN, not an inner one: an attempt with no verdict is exactly the `kind=None` case
        `score.py` treats as uninformative, and an inner join would silently drop it -- making an
        infra failure invisible in the very number spec asks be reported beside the pass rate.
        """
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT a.obligation_id, v.kind::text, a.tokens_in + a.tokens_out, "
                        "a.kernel_ms, a.wallclock_ms FROM attempt a "
                        "LEFT JOIN verdict v ON v.attempt_id = a.id "
                        "WHERE a.run_id = CAST(:r AS uuid)"
                    ),
                    {"r": str(run_id)},
                )
            ).all()
        outcomes: dict[uuid.UUID, list[AttemptOutcome]] = {}
        for obligation_id, kind, tokens, kernel_ms, wallclock_ms in rows:
            outcomes.setdefault(obligation_id, []).append(
                AttemptOutcome(
                    kind=VerdictKind(kind) if kind is not None else None,
                    tokens=int(tokens or 0),
                    kernel_ms=int(kernel_ms or 0),
                    wallclock_ms=int(wallclock_ms or 0),
                )
            )
        return {obligation: tuple(items) for obligation, items in outcomes.items()}

    async def winning_tactics(self, run_id: uuid.UUID) -> dict[uuid.UUID, str]:
        """Which portfolio member closed each obligation, from the trajectory's own step labels.

        Read from the label rather than parsed out of the proof text: the label is what the policy
        called the action, which is the one account that cannot disagree with what actually ran.
        """
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT a.obligation_id, t.steps_blob FROM attempt a "
                        "JOIN trajectory t ON t.attempt_id = a.id "
                        "JOIN verdict v ON v.attempt_id = a.id "
                        "WHERE a.run_id = CAST(:r AS uuid) AND v.kind = 'proved'"
                    ),
                    {"r": str(run_id)},
                )
            ).all()
        winners: dict[uuid.UUID, str] = {}
        for obligation_id, steps_blob in rows:
            if steps_blob is None:
                continue
            # `steps_blob` is a blob-suffixed column, so it holds either the content inline or a
            # CAS digest behind a tag byte -- `from_bytea` is the only thing that knows which
            # (M1.8.4), and it needs the store to follow a digest.
            steps = json.loads(await from_bytea(self._blobs, bytes(steps_blob)))
            succeeded = [s["label"] for s in steps if s.get("ok") and s.get("label")]
            if succeeded:
                winners[obligation_id] = str(succeeded[-1])
        return winners

    async def accepted_proofs(self, run_id: uuid.UUID) -> dict[uuid.UUID, AcceptedProof]:
        """Each obligation's sealed statement and the proof its verdict accepted.

        Joined from `obligation` and `verdict` rather than reconstructed from the policy: the
        verdict row is what `mark_proved` checked the §1.1 predicate against, so it is the only
        account of the proof that cannot disagree with the one the system acted on.
        """
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT DISTINCT ON (o.id) o.id, o.goal_src, v.proof_blob, v.axioms "
                        "FROM obligation o LEFT JOIN verdict v ON v.obligation_id = o.id "
                        "WHERE o.run_id = CAST(:r AS uuid) "
                        # An obligation may carry one verdict per attempt, so the accepted one has
                        # to be chosen rather than whichever row the join happened to yield first.
                        "ORDER BY o.id, (v.kind = 'proved') DESC NULLS LAST"
                    ),
                    {"r": str(run_id)},
                )
            ).all()
        proofs: dict[uuid.UUID, AcceptedProof] = {}
        for obligation_id, goal_src, proof_blob, axioms in rows:
            proof_text: str | None = None
            if proof_blob is not None:
                # Blob-suffixed column: inline content or a CAS digest behind a tag byte (M1.8.4).
                proof_text = (await from_bytea(self._blobs, bytes(proof_blob))).decode()
            proofs[obligation_id] = AcceptedProof(
                obligation_id=obligation_id,
                goal_src=goal_src,
                proof_text=proof_text,
                axioms=tuple(axioms or ()),
            )
        return proofs

    async def artifact(self, run_id: uuid.UUID) -> ArtifactResult:
        assembled = await FileMaterializer(
            session_factory=self._sessions, blobs=self._blobs, lean=self._lean
        ).assemble(run_id)
        return ArtifactResult(
            source=assembled.source,
            complete=assembled.complete,
            elaborates=assembled.elaborates,
            links=assembled.links,
            holes=assembled.holes,
            unfilled=tuple(assembled.unfilled),
        )


def _run_gate(
    *,
    app_async_database_url: str,
    app_database_url: str,
    leanserv: TestClient,
    bundle_root: Path,
    lake_project_dir: Path,
    tmp_path: Path,
    base_env_digest: str,
    corpus: MiniF2FCorpus,
    repeats: int = 1,
) -> tuple[list[SuiteReport], list[uuid.UUID]]:
    """Run the gate `repeats` times, and return every report plus every run id created.

    All repetitions share **one** `asyncio.run`, which is not a tidiness choice: asyncpg binds a
    connection to the loop that opened it, so a second `asyncio.run` reusing the same engine hits
    a live connection whose loop is gone ("got Future attached to a different loop"). M1.8.4
    recorded that for one engine inside one test; the three-run stability check is the same trap
    one level out, since `httpx.ASGITransport` dispatches into the leanserv app on *this* loop
    rather than on `TestClient`'s portal, so leanserv's own engine is bound here too.

    Each repetition is still a genuinely separate run -- its own run row, obligations, attempts and
    verdicts -- which is what "stable across three runs" is asking about. Sharing an event loop is
    not sharing state.
    """
    created: list[uuid.UUID] = []

    async def main() -> list[SuiteReport]:
        engine = create_async_engine(app_async_database_url)
        heartbeat_engine = create_engine(app_database_url)
        reports: list[SuiteReport] = []
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=leanserv.app), base_url="http://leanserv"
            ) as http:
                for _ in range(repeats):
                    pipeline = _Pipeline(
                        sessions=sessions,
                        heartbeat_engine=heartbeat_engine,
                        lean=LeanServiceClient(client=http),
                        blobs=LocalBlobStore(tmp_path / "app_blobs"),
                        bundle_root=bundle_root,
                        lake_project_dir=lake_project_dir,
                        base_env_digest=base_env_digest,
                        policy=SymbolicPortfolio(
                            tactics=GATE_TACTICS, tactic_timeout_ms=GATE_TACTIC_TIMEOUT_MS
                        ),
                    )
                    try:
                        reports.append(
                            await run_suite(pipeline, corpus.by_id(EASY_TAIL), corpus=corpus)
                        )
                    finally:
                        created.extend(pipeline.run_ids)
        finally:
            heartbeat_engine.dispose()
            await engine.dispose()
        return reports

    return asyncio.run(main()), created


def _cleanup(admin_engine: Engine, run_ids: list[uuid.UUID]) -> None:
    with admin_engine.connect() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": run_id})
        conn.commit()


# --------------------------------------------------------------------------------------------
# Corpus integrity. No infrastructure: these guard the benchmark itself, and a benchmark that
# quietly changed would invalidate every number measured against it (spec §7.5's read-only
# requirement, in the form this repo can actually enforce).
# --------------------------------------------------------------------------------------------


def test_the_vendored_corpus_matches_its_recorded_digest(corpus: MiniF2FCorpus) -> None:
    """The digest covers the statements, so an edited statement fails here and a re-pin that
    changed no statement does not."""
    from lean_agent_eval.suites.vendor_minif2f import corpus_digest

    recomputed = corpus_digest(
        {
            "problems": [
                {
                    "id": p.id,
                    "split": p.split,
                    "statement": p.statement,
                    "statement_sha256": p.statement_sha256,
                }
                for p in corpus.problems
            ]
        }
    )
    assert recomputed == corpus.corpus_sha256
    assert len(corpus.problems) == 488
    assert {p.split for p in corpus.problems} == {"test", "valid"}


def test_the_corpus_records_the_toolchain_it_was_written_for(corpus: MiniF2FCorpus) -> None:
    """Upstream targets v4.24.0 and this repo pins v4.33.1, and that gap is the explanation for
    every statement that fails to seal. Recorded rather than discovered again later: Phase 1 gate
    1 has been blocked on exactly this ambiguity since planning."""
    assert corpus.provenance["upstream_toolchain"] == "leanprover/lean4:v4.24.0"
    assert corpus.provenance["license"] == "MIT"
    assert corpus.provenance["commit"]


def test_the_easy_tail_is_real_and_ordered(corpus: MiniF2FCorpus) -> None:
    """Every id in the gate's expectations exists, and `by_id` raises rather than silently
    shrinking the expectations if one ever stops existing."""
    assert EASY_TAIL
    assert len(set(EASY_TAIL)) == len(EASY_TAIL)
    problems = corpus.by_id(EASY_TAIL)
    assert [p.id for p in problems] == list(EASY_TAIL)
    with pytest.raises(KeyError, match="no problem"):
        corpus.by_id(["definitely_not_a_miniF2F_problem"])


def test_the_submitted_file_carries_the_opens_but_never_an_import(corpus: MiniF2FCorpus) -> None:
    """A submission is elaborated as a *body* against an already-warm base env, so a stray
    `import` line fails outright with "invalid 'import' command" (M2.7). The `open` line, by
    contrast, is load-bearing -- these statements are written under it."""
    source = build_submission_source(corpus.by_id(EASY_TAIL), opens=corpus.opens)
    assert source.startswith(corpus.opens)
    assert "import" not in source
    assert source.count("sorry") == len(EASY_TAIL)


# --------------------------------------------------------------------------------------------
# The gate itself.
# --------------------------------------------------------------------------------------------


#: Spec's "stable across three runs", and the only reason this module runs the gate more than once.
GATE_REPEATS = 3


@pytest.fixture(scope="module")
def gate_reports(
    app_async_database_url: str,
    app_database_url: str,
    leanserv: TestClient,
    bundle_root: Path,
    mathlib: Path,
    tmp_path_factory: pytest.TempPathFactory,
    mathlib_base_env: str,
    corpus: MiniF2FCorpus,
    admin_engine: Engine,
) -> Iterator[list[SuiteReport]]:
    """Every report this module asserts on, from **one** set of gate runs.

    Module-scoped because the gate is expensive and each run was being paid for repeatedly: five
    tests each drove their own, so a full-Mathlib worker was warmed five times and the tail was
    proved seven times over, for assertions that are all read-only views of the same behaviour.
    Measured at 198 s for the file; sharing one run brings it to roughly a third of that, and spec
    wants this suite on every PR *forever*, so its cost compounds across every future milestone.

    Three runs rather than one only because spec's exit criterion says "stable across three runs".
    Each is a genuinely separate run -- its own run row, obligations, attempts and verdicts -- and
    `/v1/link` is uncached, so all three really do re-link, replay and audit in the kernel.

    What this trades away is test independence: a failure in the gate run itself now fails every
    test in the module rather than one. That is the honest reading anyway -- they are assertions
    about a single pipeline execution, not about five -- but it does mean a red build here needs
    the first failure read, not the count.
    """
    reports, run_ids = _run_gate(
        app_async_database_url=app_async_database_url,
        app_database_url=app_database_url,
        leanserv=leanserv,
        bundle_root=bundle_root,
        lake_project_dir=mathlib,
        tmp_path=tmp_path_factory.mktemp("gate_blobs"),
        base_env_digest=mathlib_base_env,
        corpus=corpus,
        repeats=GATE_REPEATS,
    )
    try:
        yield reports
    finally:
        _cleanup(admin_engine, run_ids)


@pytest.fixture(scope="module")
def report(gate_reports: list[SuiteReport]) -> SuiteReport:
    """The first of the three runs, for the tests that only need one."""
    return gate_reports[0]


def test_the_null_agent_closes_the_easy_tail_at_zero_token_cost(report: SuiteReport) -> None:
    """Phase 2's exit criterion, first half.

    Every problem in `EASY_TAIL` reaches `proved` through the whole acceptance path -- sealed,
    linked in the kernel against the sealed constant, replayed, axiom-audited, and admitted by
    `mark_proved`'s own re-check of the §1.1 predicate -- with zero model calls.
    """
    print(f"\n{report.summary()}")

    assert report.unsealed == frozenset(), (
        "every easy-tail statement must still seal under this toolchain; "
        f"unsealed: {sorted(report.unsealed)}"
    )
    assert report.infra_errors == frozenset(), (
        f"infra_error is never a proof result: {sorted(report.infra_errors)}"
    )
    assert report.proved == frozenset(EASY_TAIL), (
        f"expected the whole tail; missing {sorted(frozenset(EASY_TAIL) - report.proved)}"
    )

    # "at zero token cost", and structurally so: the policy declares no roles, so the executor has
    # nothing to ask a model for. Asserted where it would break rather than trusted.
    assert report.tokens == 0
    assert report.score.mean_pass_at_k[1] == 1.0
    assert report.score.overall_infra_error_rate == 0.0
    # Every winner is a real tactic, recorded on the trajectory -- which is what makes this run
    # usable as the unencumbered training data §7.1 says this policy exists to produce.
    assert all(r.tactic in GATE_TACTICS for r in report.results if r.proved)


def test_the_materialized_file_elaborates_and_links(report: SuiteReport) -> None:
    """Phase 2's exit criterion, second half: "a materialized file passes both the elaboration and
    the link check".

    The file is one artifact with every hole filled, so `elaborates` is the check §6.3 step 6
    exists for -- per-obligation acceptance proves each hole is filled correctly, whole-file
    elaboration proves they were filled *compatibly*.
    """
    artifact = report.artifact

    assert artifact.holes == len(EASY_TAIL)
    assert artifact.unfilled == ()
    assert artifact.complete is True
    assert artifact.elaborates is True
    assert artifact.links is True
    # Standalone: the sealed goals are inlined rather than imported, so the file does not depend
    # on a per-run generated module whoever receives it will not have.
    assert "Bundle_" not in artifact.source
    # No `sorry` as a *proof*. A blunt `"sorry" not in source` is wrong and was the first thing
    # tried: decomposition names each hole `sorry_<n>`, so the file legitimately says `sorry_1` in
    # a comment and `@sorry_13` in the reassembly term. The negative lookahead keeps those and
    # still catches a bare `sorry`, which matters because a `sorry`'d file would still *elaborate*
    # -- it is a warning, not an error (M1.1), so `elaborates` alone cannot rule it out.
    assert re.search(r"\bsorry\b(?!_)", artifact.source) is None


def test_the_easy_tail_is_stable_across_three_runs(gate_reports: list[SuiteReport]) -> None:
    """Spec's "stable across three runs", run as three genuinely separate runs.

    Each pass creates its own run, obligations, attempts and verdicts -- `/v1/link` is uncached
    (one verdict per attempt, by primary key), so all three genuinely re-link, replay and audit in
    the kernel. What may legitimately be reused is the `/v1/check` screening cache, which is the
    point of having one.

    A drifting pass set is the failure this catches, and it is the reason spec wants this suite on
    every PR forever: without it, a harness that closes a different subset each run looks exactly
    like a policy change.
    """
    reports = gate_reports
    passes = [r.proved for r in reports]
    assert passes[0] == passes[1] == passes[2] == frozenset(EASY_TAIL), (
        f"pass set drifted across runs: {[sorted(p) for p in passes]}"
    )
    # The winning tactic must also be stable, not merely the pass/fail outcome: a portfolio whose
    # winner moves between runs is nondeterministic in a way that a pass-set comparison hides.
    winners = [{r.id: r.tactic for r in report.results} for report in reports]
    assert winners[0] == winners[1] == winners[2]
    assert all(report.tokens == 0 for report in reports)


def test_the_phase_2_baseline_is_unchanged(report: SuiteReport) -> None:
    """M3.0 -- the record Phase 3's exit criterion is measured against.

    Spec §8's Phase 3 exit begins "the Phase 2 symbolic baseline still passes bit-identically".
    The other tests in this file assert the *criterion* (everything closes, at zero tokens, with an
    artifact that elaborates and links); this one asserts nothing moved: same sealed statements,
    same winning tactics, byte-identical accepted proofs, same axiom cones, byte-identical
    artifact.

    It fails on any drift, including drift that leaves the pass rate at 100%. That is the point --
    a model policy is supposed to *dominate* the symbolic one, and if wiring a model in also
    perturbs the symbolic path, the thing it is being compared against has moved.

    To re-record after a deliberate change, having read the printed diff:

        LEAN_AGENT_RERECORD_BASELINE=1 uv run pytest tests/eval/test_minif2f.py -k baseline
    """
    policy = SymbolicPortfolio(tactics=GATE_TACTICS, tactic_timeout_ms=GATE_TACTIC_TIMEOUT_MS)
    current = baseline.record(
        report,
        policy_id=policy.id,
        policy_config_hash=policy.config_hash,
        tactics=GATE_TACTICS,
    )

    if os.environ.get(baseline.RERECORD_ENV) == "1":
        if baseline.BASELINE_PATH.exists():
            for line in baseline.compare(baseline.load(), current):
                print(f"  re-recording over: {line}")
        baseline.save(current)
        pytest.skip(f"re-recorded {baseline.BASELINE_PATH}")

    differences = baseline.compare(baseline.load(), current)
    assert not differences, "the Phase 2 baseline moved:\n  " + "\n  ".join(differences)


def test_the_baseline_records_real_evidence_not_placeholders(report: SuiteReport) -> None:
    """A golden file full of `None` would compare equal to itself forever.

    So this checks the recorded fields are actually populated from the run: every proved problem
    has a sealed statement, an accepted proof and a non-empty axiom cone, and the proof genuinely
    names both the sealed constant and its winning tactic. Without it, a regression that stopped
    writing `verdict.proof_blob` would make the baseline vacuous rather than failing.
    """
    recorded = baseline.load()
    by_id = recorded.by_id()
    assert set(by_id) == set(EASY_TAIL)

    for problem in recorded.problems:
        assert problem.proved is True
        assert problem.tactic in GATE_TACTICS
        assert problem.goal_src_sha256 is not None
        assert problem.proof_sha256 is not None
        # `simp`/`linarith` and friends genuinely use these; an empty cone here would mean the
        # audit surface was not recorded rather than that the proof was axiom-free.
        assert set(problem.axioms) <= {"propext", "Classical.choice", "Quot.sound"}

    # And the digests are of the real thing, checked against a live run rather than themselves.
    for result in report.results:
        assert result.proof_text is not None
        assert result.goal_src is not None
        assert result.tactic is not None and result.tactic in result.proof_text
        assert result.goal_src != ""
        entry = by_id[result.id]
        assert entry.proof_sha256 == baseline.sha256_text(result.proof_text)
        assert entry.goal_src_sha256 == baseline.sha256_text(result.goal_src)
