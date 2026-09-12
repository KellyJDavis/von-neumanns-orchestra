"""Rank Phase 3's three open-weights provers on miniF2F, through the whole pipeline (M3.12).

Run by hand, never from CI: it needs a live vLLM serving one prover at a time, and at a prover's
published sample budget it is hours of generation per prover.

    vllm serve <model> --port 8766 --max-model-len 40960 --generation-config vllm
    export LEAN_AGENT_APP_DATABASE_URL=postgresql+asyncpg://app:<pw>@localhost:5432/leanagent
    export LEAN_AGENT_LEANSERV_DATABASE_URL=postgresql+asyncpg://leanserv:<pw>@localhost:5432/leanagent
    uv run python -m lean_agent_eval.suites.prover_eval run --prover goedel-8b \\
        --endpoint http://127.0.0.1:8766 --samples 32 --out reports/
    # ... once per prover, and once as `--prover symbolic` (no endpoint) for the dominance check
    uv run python -m lean_agent_eval.suites.prover_eval rank reports/

**Nothing here stands in for anything.** The problems go through what a deployment runs: one
multi-`sorry` submission is ingested (sealed, admission signals and all), its bundle materialized,
and the real control loop claims each obligation and runs `WholeProofSampler` through the real
executor, completions service, `/v1/check` and `/v1/link`. A problem counts as proved only when its
obligation reached `proved`, which only `mark_proved` can set. What this module adds is the prover
specs, the benchmark's informal statements in the context, and reading the outcome back.

**Each prover runs the way its published numbers were produced**, as far as that is documented:
its own prompt layout (`ProverFormat`), its own sampling -- `top_k` included, stated rather than left
to vLLM's generation-config default -- and the informal statement its prompts carry. Where this
system cannot follow, the divergence is stated in every report's manifest (`DIVERGENCES`), not
left for a reader to discover.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime
import hashlib
import json
import os
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from lean_agent_api.ingestion import Ingestor, Submission
from lean_agent_api.materialize import BundleMaterializer
from lean_agent_core.actions import Budget, ObligationContext
from lean_agent_core.blobs import LocalBlobStore, from_bytea
from lean_agent_core.codecs import decode_trajectory_token_ids
from lean_agent_core.enums import AttemptStatus, ObligationStatus, ProvenanceClass, VerdictKind
from lean_agent_core.executor import (
    CompletionService,
    PolicyExecutor,
    TrajectoryWriter,
    load_context,
)
from lean_agent_core.protocols import BlobStore, LeanService, Policy, SamplingParams
from lean_agent_core.roles import ModelRole
from lean_agent_core.scheduler import ClaimedAttempt
from lean_agent_core.state import ObligationOutcome
from lean_agent_core.worker import AttemptResult, InfraError, Worker
from lean_agent_models.completions import RoutedCompletions
from lean_agent_models.config import BackendConfig
from lean_agent_models.errors import ModelTimeout, ModelUnavailable
from lean_agent_models.router import ModelRouter, build_backend
from lean_agent_models.template import load_chat_tokenizer
from lean_agent_policies.symbolic import SymbolicPortfolio
from lean_agent_policies.whole_proof import ProverFormat, WholeProofSampler
from lean_agent_serv.api import create_app
from lean_agent_serv.cache import VerificationCacheStore
from lean_agent_serv.client import LeanServiceClient
from lean_agent_serv.pool import LeanReplPool, PoolConfig
from lean_agent_serv.verdicts import VerdictWriter
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from lean_agent_eval.ranking import (
    ProblemOutcome,
    ProverRun,
    compare_absolute,
    dominance,
    rank,
    sample_problems,
)
from lean_agent_eval.suites.minif2f import (
    CORPUS_OPENS,
    InformalStatements,
    MiniF2FProblem,
    build_submission_source,
    load_corpus,
    load_informal,
)

#: The provers' shared hard limit (M3.12): each request's `max_tokens` is capped to what its
#: prompt leaves of it, which is how Goedel-Prover-V2's own pipeline runs.
CONTEXT_TOKENS = 40_960

#: The published header's imports (DeepSeek-Prover's miniF2F, which Goedel's dataset carries), so
#: the prompt's header matches it line for line. `Aesop` is already inside `Mathlib`; importing it
#: again changes nothing the kernel sees, only what the model is shown.
BASE_ENV_IMPORTS = ("Mathlib", "Aesop")

#: One converted tokenizer serves all three provers -- measured, not assumed: each prover's own
#: `apply_chat_template` renders every prompt format in the ranking to exactly the ids this
#: artifact produces (see CLAUDE.md's M3.12 notes).
TOKENIZER_SHA256 = "41e00eccf531cffc2e562d38bdd879d41e5044ea279af5b73c6a32aabcc8fe04"

SEED = 1234

#: Endpoint failures tolerated per obligation before the attempt is charged, so a dead server ends
#: a run rather than looping on it. The problem is reported as an infra failure either way.
MAX_ENDPOINT_RETRIES = 2

#: What this harness cannot do the way the published evaluations did, and says so in every report.
DIVERGENCES = (
    (
        "maxHeartbeats 400000 is shown and set where every published prompt shows 0 "
        "(spec §7.2 denies 0)."
    ),
    (
        "The prompt shows the sealed ∀-statement under a generated name (G_n), not the problem's own "
        "signature and name: the sealed statement is the only one an obligation has (spec §1.1)."
    ),
    (
        "Lean v4.33.1 with Mathlib 0df444a3, not the provers' Lean v4.9 setups; statements that no "
        "longer round-trip through decomposition are unsealed and not scored (M2.10)."
    ),
    (
        "Acceptance is link + replay + axiom audit against the sealed goal, not compiling the "
        "model's file; the model's theorem must inhabit the sealed constant."
    ),
    (
        "pass@n counts one link per problem: the first sample that passes /v1/check is linked, and "
        "a later sample is never tried if that link is refused (reported as link_rejected)."
    ),
)


@dataclass(frozen=True)
class ProverSpec:
    """One prover as its published numbers were produced, and those numbers."""

    key: str
    model_id: str
    #: The Hugging Face commit the weights were downloaded at, recorded in the manifest (§7.3).
    weights_revision: str
    prompt_format: Callable[[], ProverFormat]
    temperature: float
    top_p: float
    top_k: int
    published_pass_at_32: float
    published_source: str
    sampling_source: str

    def policy(self) -> WholeProofSampler:
        return WholeProofSampler(prompt_format=self.prompt_format())

    def sampling(self, samples: int) -> SamplingParams:
        return SamplingParams(
            temperature=self.temperature,
            top_p=self.top_p,
            top_k=self.top_k,
            max_tokens=CONTEXT_TOKENS,
            n=samples,
        )


PROVERS: dict[str, ProverSpec] = {
    spec.key: spec
    for spec in (
        ProverSpec(
            key="goedel-8b",
            model_id="Goedel-LM/Goedel-Prover-V2-8B",
            weights_revision="dfd02e6271a58375dfbf3ece0175277cf6b6a89a",
            prompt_format=ProverFormat.goedel_pipeline,
            temperature=1.0,
            top_p=0.95,
            top_k=0,
            published_pass_at_32=84.6,
            published_source="arXiv:2508.03613, abstract (its model card says 83.0)",
            sampling_source=(
                "Goedel-Prover-V2 scripts/pipeline.sh (TEMPERATURE=1.0) and src/inference.py "
                "(top_p=0.95, top_k unset), run through vLLM's offline API"
            ),
        ),
        ProverSpec(
            key="pythagoras-4b",
            model_id="Pythagoras-LM/Pythagoras-Prover-4B",
            weights_revision="aa05cf9a86cd1bc5af16935ab8f2190f4a1e62b8",
            prompt_format=ProverFormat.pythagoras_card,
            temperature=0.6,
            top_p=0.95,
            top_k=20,
            published_pass_at_32=86.07,
            published_source="model card and arXiv:2606.12594 (Kimina-revised miniF2F)",
            sampling_source=(
                "its generation_config.json (temperature 0.6, top_p 0.95, top_k 20), which its "
                "card's transformers snippet samples with"
            ),
        ),
        ProverSpec(
            key="kimina-distill-8b",
            model_id="AI-MO/Kimina-Prover-Distill-8B",
            weights_revision="74d328a7b1f001ab4871812582fc66d9bf70c68b",
            prompt_format=ProverFormat.kimina_card,
            temperature=0.6,
            top_p=0.95,
            top_k=0,
            published_pass_at_32=77.86,
            published_source="model card",
            sampling_source=(
                "its card's vLLM SamplingParams(temperature=0.6, top_p=0.95) -- top_k unset, "
                "which vLLM's offline API leaves disabled"
            ),
        ),
    )
}


class EvalError(RuntimeError):
    """The run cannot measure what it claims to -- refused rather than reported."""


# --------------------------------------------------------------------------------------------
# Infrastructure: the same components a deployment runs, wired in one process.
# --------------------------------------------------------------------------------------------


@dataclass
class Infra:
    sessions: async_sessionmaker[AsyncSession]
    heartbeat_engine: Engine
    lean: LeanService
    blobs: BlobStore
    bundle_root: Path
    lake_project_dir: Path
    base_env_digest: str
    base_env: dict[str, Any]


class _Throttled(httpx.AsyncBaseTransport):
    """At most `limit` Lean requests in flight.

    The pool's worker cap is soft -- it spawns past the cap rather than block (M1.8.3) -- and a
    full-Mathlib worker is ~6 GiB, so concurrent attempts all checking at once would otherwise each
    spawn one, beside a vLLM holding most of the machine. Generation, not checking, is what takes
    the time, so throttling Lean costs almost nothing.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, limit: int) -> None:
        self._inner = inner
        self._gate = asyncio.Semaphore(limit)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async with self._gate:
            return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _base_env_recipe(lake_project_dir: Path) -> dict[str, Any]:
    manifest = json.loads((lake_project_dir / "lake-manifest.json").read_text())
    return {
        "imports": list(BASE_ENV_IMPORTS),
        "toolchain": (lake_project_dir / "lean-toolchain").read_text().strip(),
        "mathlib": next(p["rev"] for p in manifest["packages"] if p["name"] == "mathlib"),
    }


async def _register_base_env(
    sessions: async_sessionmaker[AsyncSession], recipe: dict[str, Any]
) -> str:
    """Content-addressed over the recipe *and* the toolchain it was built for, so a bump makes a
    new base env rather than silently reusing verdicts cached against the old one (§7.6)."""
    digest = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).digest()
    async with sessions() as session:
        await session.execute(
            text(
                "INSERT INTO base_env (digest, recipe, toolchain_rev, mathlib_rev) "
                "VALUES (:d, CAST(:r AS jsonb), :t, :m) ON CONFLICT (digest) DO NOTHING"
            ),
            {
                "d": digest,
                "r": json.dumps({"imports": recipe["imports"]}),
                "t": recipe["toolchain"],
                "m": recipe["mathlib"],
            },
        )
        await session.commit()
    return digest.hex()


@asynccontextmanager
async def infrastructure(
    *,
    app_url: str,
    leanserv_url: str,
    lake_project_dir: Path,
    bundle_root: Path,
    blob_root: Path,
    lean_concurrency: int,
    max_total_workers: int = 2,
) -> AsyncIterator[Infra]:
    """Everything is created and disposed inside the caller's one event loop -- asyncpg binds a
    connection to the loop that opened it (M1.8.4), and `httpx.ASGITransport` runs leanserv on the
    calling loop, so its pool is closed here too rather than by an ASGI lifespan that never runs.

    `max_total_workers` is a memory decision: two full-Mathlib workers (~12 GiB) lets ingestion's
    and linking's pool keys stay warm together on a workstation; a 16 GiB CI runner takes one.
    """
    app_engine = create_async_engine(app_url)
    heartbeat = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    leanserv_engine = create_async_engine(leanserv_url)
    blobs = LocalBlobStore(blob_root)
    leanserv_sessions = async_sessionmaker(leanserv_engine, expire_on_commit=False)
    pool = LeanReplPool(
        lake_project_dir, PoolConfig(max_total_workers=max_total_workers, bundle_root=bundle_root)
    )
    app = create_app(
        pool,
        VerificationCacheStore(leanserv_sessions, blobs),
        VerdictWriter(leanserv_sessions, blobs),
        leanserv_sessions,
    )
    http = httpx.AsyncClient(
        transport=_Throttled(httpx.ASGITransport(app=app), lean_concurrency),
        base_url="http://leanserv",
    )
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    try:
        recipe = _base_env_recipe(lake_project_dir)
        yield Infra(
            sessions=sessions,
            heartbeat_engine=heartbeat,
            lean=LeanServiceClient(client=http),
            blobs=blobs,
            bundle_root=bundle_root,
            lake_project_dir=lake_project_dir,
            base_env_digest=await _register_base_env(sessions, recipe),
            base_env=recipe,
        )
    finally:
        await http.aclose()
        await pool.aclose()
        heartbeat.dispose()
        await app_engine.dispose()
        await leanserv_engine.dispose()


# --------------------------------------------------------------------------------------------
# The prover side.
# --------------------------------------------------------------------------------------------


async def check_server(http: httpx.AsyncClient, model_id: str) -> dict[str, Any]:
    """The endpoint must serve *this* model at *this* window before anything is spent.

    `context_tokens` has to equal the server's `--max-model-len`: vLLM rejects a request that does
    not fit its own window, so a smaller one fails requests ours sends, and a larger one leaves
    answers capped shorter than they could be (M3.12).
    """
    listing = (await http.get("/v1/models")).json()["data"]
    served = next((m for m in listing if m.get("id") == model_id), None)
    if served is None:
        raise EvalError(f"{http.base_url} serves {[m.get('id') for m in listing]}, not {model_id}")
    if served.get("max_model_len") != CONTEXT_TOKENS:
        raise EvalError(
            f"{model_id} is served with max_model_len={served.get('max_model_len')}; this "
            f"evaluation needs {CONTEXT_TOKENS} (vllm serve ... --max-model-len {CONTEXT_TOKENS})"
        )
    version = await http.get("/version")
    return {
        "max_model_len": served["max_model_len"],
        "serving_version": f"vllm=={version.json()['version']}"
        if version.status_code == 200
        else None,
    }


async def model_service(
    spec: ProverSpec,
    *,
    samples: int,
    tokenizer_dir: Path,
    http: httpx.AsyncClient,
) -> tuple[RoutedCompletions, dict[str, Any]]:
    """`check_server`, then `build_service` -- what a live run does."""
    served = await check_server(http, spec.model_id)
    return build_service(
        spec,
        samples=samples,
        tokenizer_dir=tokenizer_dir,
        http=http,
        serving_version=served["serving_version"],
    )


def build_service(
    spec: ProverSpec,
    *,
    samples: int,
    tokenizer_dir: Path,
    http: httpx.AsyncClient,
    serving_version: str | None,
) -> tuple[RoutedCompletions, dict[str, Any]]:
    """The completions service for one prover, and its manifest entry (§7.3). Separate from the
    server check so a replay -- whose server is a recording -- builds exactly what a live run does."""
    config = BackendConfig(
        role=ModelRole.PROVER,
        backend="vllm",
        model_id=spec.model_id,
        provenance=ProvenanceClass.OPEN_WEIGHTS,
        endpoint=str(http.base_url),
        tokenizer_dir=tokenizer_dir,
        tokenizer_revision=TOKENIZER_SHA256,
        weights_revision=spec.weights_revision,
        serving_version=serving_version,
        seed=SEED,
        sampling=spec.sampling(samples),
        context_tokens=CONTEXT_TOKENS,
    )
    router = ModelRouter(
        backends={ModelRole.PROVER: build_backend(config, client=http)},
        configs={ModelRole.PROVER: config},
    )
    service = RoutedCompletions(
        router=router,
        tokenizers={
            ModelRole.PROVER: load_chat_tokenizer(tokenizer_dir, expect_sha256=TOKENIZER_SHA256)
        },
    )
    return service, router.manifest_entries()[0]


Runner = Callable[[ClaimedAttempt], Any]


def endpoint_trouble_is_infra(runner: Runner, *, retries: int = MAX_ENDPOINT_RETRIES) -> Runner:
    """A slow or unreachable model server is infrastructure, not a failed proof.

    `ModelBackendError` is an ordinary exception, so the control loop charges it as a failed attempt
    (M2.4) -- right when a *policy* misbehaves, wrong for a server that timed out, which says nothing
    about the problem. Here a timeout or an unreachable endpoint is an `InfraError`: unbudgeted, the
    obligation reopened. Past `retries` on one obligation the attempt is charged instead, so a dead
    server ends the run rather than looping -- and `read_outcomes` still reports that problem as an
    infra failure, never as the prover's miss.
    """
    trouble: Counter[uuid.UUID] = Counter()

    async def run(claimed: ClaimedAttempt) -> AttemptResult:
        try:
            result: AttemptResult = await runner(claimed)
            return result
        except (ModelTimeout, ModelUnavailable) as exc:
            trouble[claimed.obligation_id] += 1
            if trouble[claimed.obligation_id] > retries:
                return AttemptResult(
                    outcome=ObligationOutcome.RETRYABLE_FAILURE,
                    detail=f"endpoint trouble on {retries + 1} attempts: {exc}",
                )
            raise InfraError(f"{type(exc).__name__}: {exc}") from exc

    return run


# --------------------------------------------------------------------------------------------
# One prover over a problem set.
# --------------------------------------------------------------------------------------------


async def _problem_by_obligation(
    sessions: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    problems: Sequence[MiniF2FProblem],
) -> dict[uuid.UUID, str]:
    """Ingestion names goals `G_1`, `G_2`, ... in submission order, and a goal that does not seal
    has no obligation -- so the index in the name is the alignment, as in `minif2f.run_suite`."""
    async with sessions() as session:
        rows = (
            await session.execute(
                text("SELECT id, decl_name FROM obligation WHERE run_id = CAST(:r AS uuid)"),
                {"r": str(run_id)},
            )
        ).all()
    return {oid: problems[int(str(decl).rsplit("_", 1)[-1]) - 1].id for oid, decl in rows}


async def read_outcomes(
    sessions: async_sessionmaker[AsyncSession],
    blobs: BlobStore,
    run_id: uuid.UUID,
    problems: Sequence[MiniF2FProblem],
    problem_of: dict[uuid.UUID, str],
) -> tuple[ProblemOutcome, ...]:
    async with sessions() as session:
        statuses = dict(
            (
                await session.execute(
                    text("SELECT id, status::text FROM obligation WHERE run_id = CAST(:r AS uuid)"),
                    {"r": str(run_id)},
                )
            )
            .tuples()
            .all()
        )
        attempts = (
            await session.execute(
                text(
                    "SELECT a.obligation_id, a.status::text, v.kind::text, "
                    "a.tokens_in + a.tokens_out, a.kernel_ms, a.wallclock_ms, t.token_ids_blob "
                    "FROM attempt a LEFT JOIN verdict v ON v.attempt_id = a.id "
                    "LEFT JOIN trajectory t ON t.attempt_id = a.id "
                    "WHERE a.run_id = CAST(:r AS uuid)"
                ),
                {"r": str(run_id)},
            )
        ).all()

    per: dict[uuid.UUID, Counter[str]] = {}
    for obligation_id, status, kind, tokens, kernel_ms, wallclock_ms, ids_blob in attempts:
        acc = per.setdefault(obligation_id, Counter())
        acc["attempts"] += 1
        acc["infra"] += status == AttemptStatus.INFRA_ERROR.value
        acc["link_rejected"] += kind is not None and kind != VerdictKind.PROVED.value
        acc["tokens"] += int(tokens or 0)
        acc["kernel_ms"] += int(kernel_ms or 0)
        acc["wallclock_ms"] += int(wallclock_ms or 0)
        if ids_blob is not None:
            for exchange in decode_trajectory_token_ids(await from_bytea(blobs, bytes(ids_blob))):
                acc["samples"] += len(exchange.completions)
                acc["truncated"] += sum(1 for r in exchange.finish_reasons if r == "length")

    obligation_of = {problem: obligation for obligation, problem in problem_of.items()}
    outcomes: list[ProblemOutcome] = []
    for problem in problems:
        obligation = obligation_of.get(problem.id)
        if obligation is None:
            outcomes.append(ProblemOutcome(problem_id=problem.id, sealed=False, proved=False))
            continue
        acc = per.get(obligation, Counter())
        # The obligation's own status, which only `mark_proved` can set -- not a verdict row.
        proved = statuses.get(obligation) == ObligationStatus.PROVED.value
        outcomes.append(
            ProblemOutcome(
                problem_id=problem.id,
                sealed=True,
                proved=proved,
                # Never attempted, or only ever failed for infrastructure: nothing is known.
                infra_error=not proved and (acc["attempts"] == 0 or acc["infra"] > 0),
                samples=acc["samples"],
                truncated=acc["truncated"],
                tokens=acc["tokens"],
                kernel_ms=acc["kernel_ms"],
                wallclock_ms=acc["wallclock_ms"],
                link_rejected=acc["link_rejected"] > 0,
            )
        )
    return tuple(outcomes)


async def _drain(worker: Worker) -> None:
    while await worker.run_once():
        pass


@dataclass(frozen=True)
class Prepared:
    """A submission ingested and materialized, ready to prove.

    Split from proving because the two phases want different Lean workers -- ingestion the base
    env's, proving the bundle's -- so several runs of the same problems can do all of one before any
    of the other: one warm-up per phase rather than one per run, which is what lets the replay test
    fit a CI runner's single full-Mathlib worker. Identical problem sets seal to an identical,
    content-addressed bundle, so every such run proves against the same bundle worker.
    """

    run_id: uuid.UUID
    tenant: uuid.UUID
    problems: tuple[MiniF2FProblem, ...]
    problem_of: dict[uuid.UUID, str]
    prepare_s: float


async def prepare(infra: Infra, problems: Sequence[MiniF2FProblem], *, policy_id: str) -> Prepared:
    started = time.monotonic()
    tenant = uuid.uuid4()
    ingested = await Ingestor(
        session_factory=infra.sessions, lean=infra.lean, blobs=infra.blobs
    ).ingest(
        Submission(
            base_env_digest=infra.base_env_digest,
            tenant_id=tenant,
            source=build_submission_source(problems, opens=CORPUS_OPENS),
            policy=policy_id,
            # One attempt of n samples per problem: pass@n, as published pass@32 is defined.
            budget_attempts=1,
        )
    )
    await BundleMaterializer(
        session_factory=infra.sessions,
        blobs=infra.blobs,
        bundle_root=infra.bundle_root,
        lake_project_dir=infra.lake_project_dir,
    ).materialize_run(ingested.run_id)
    return Prepared(
        run_id=ingested.run_id,
        tenant=tenant,
        problems=tuple(problems),
        problem_of=await _problem_by_obligation(infra.sessions, ingested.run_id, problems),
        prepare_s=time.monotonic() - started,
    )


async def prove(
    infra: Infra,
    prepared: Prepared,
    *,
    prover: str,
    policy: Policy,
    completions: CompletionService | None,
    informal: InformalStatements,
    samples: int,
    concurrency: int,
    manifest: dict[str, Any],
) -> ProverRun:
    problem_of = prepared.problem_of

    async def context(
        obligation_id: uuid.UUID, run_id: uuid.UUID
    ) -> tuple[ObligationContext, Budget]:
        ctx, budget = await load_context(infra.sessions, obligation_id, run_id)
        statement = informal.for_problem(problem_of[obligation_id])
        return dataclasses.replace(ctx, informal_statement=statement), budget

    executor = PolicyExecutor(
        policy=policy,
        lean=infra.lean,
        trajectories=TrajectoryWriter(infra.sessions, infra.blobs),
        context_loader=context,
        completions=completions,
    )
    runner = endpoint_trouble_is_infra(executor.runner())
    workers = [
        Worker(
            worker_id=f"{prover}-{i}",
            session_factory=infra.sessions,
            heartbeat_engine=infra.heartbeat_engine,
            runner=runner,
            policy_id=policy.id,
            policy_config_hash=policy.config_hash,
            eligible_tenants=[prepared.tenant],
        )
        for i in range(concurrency)
    ]
    started = time.monotonic()
    await asyncio.gather(*(_drain(worker) for worker in workers))
    proving_s = time.monotonic() - started

    outcomes = await read_outcomes(
        infra.sessions, infra.blobs, prepared.run_id, prepared.problems, problem_of
    )
    return ProverRun(
        prover=prover,
        samples_per_problem=samples,
        outcomes=outcomes,
        manifest={
            **manifest,
            "run_id": str(prepared.run_id),
            "tenant_id": str(prepared.tenant),
            "prepare_s": round(prepared.prepare_s, 1),
            "proving_s": round(proving_s, 1),
        },
    )


async def evaluate(
    infra: Infra,
    problems: Sequence[MiniF2FProblem],
    *,
    prover: str,
    policy: Policy,
    completions: CompletionService | None,
    informal: InformalStatements,
    samples: int,
    concurrency: int,
    manifest: dict[str, Any],
) -> ProverRun:
    """`prepare`, then `prove` -- one prover over one problem set."""
    prepared = await prepare(infra, problems, policy_id=policy.id)
    return await prove(
        infra,
        prepared,
        prover=prover,
        policy=policy,
        completions=completions,
        informal=informal,
        samples=samples,
        concurrency=concurrency,
        manifest=manifest,
    )


# --------------------------------------------------------------------------------------------
# Command line.
# --------------------------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[5]


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise EvalError(f"set {name} (an asyncpg URL for that role; see the module docstring)")
    return value


def _select(args: argparse.Namespace) -> list[MiniF2FProblem]:
    corpus = load_corpus()
    if args.problems:
        return list(corpus.by_id(args.problems.split(",")))
    split = [p.id for p in corpus.problems if p.split == args.split]
    return list(corpus.by_id(sample_problems(split, args.limit or len(split), seed=args.seed)))


def _run(args: argparse.Namespace) -> int:
    corpus = load_corpus()
    informal = load_informal()
    problems = _select(args)
    samples = 0 if args.prover == "symbolic" else args.samples

    async def main() -> ProverRun:
        async with infrastructure(
            app_url=_env("LEAN_AGENT_APP_DATABASE_URL"),
            leanserv_url=_env("LEAN_AGENT_LEANSERV_DATABASE_URL"),
            lake_project_dir=args.lake_project_dir,
            bundle_root=args.bundle_root,
            blob_root=args.blob_root,
            lean_concurrency=args.lean_concurrency,
        ) as infra:
            manifest: dict[str, Any] = {
                "suite": "miniF2F",
                "corpus_sha256": corpus.corpus_sha256,
                "informal_sha256": informal.informal_sha256,
                "split": args.split,
                "selection": {"problems": args.problems, "limit": args.limit, "seed": args.seed},
                "base_env": infra.base_env,
                "divergences": list(DIVERGENCES),
                "concurrency": args.concurrency,
                "started_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            }
            if args.prover == "symbolic":
                policy = SymbolicPortfolio()
                manifest["policy"] = {"id": policy.id, "config_hash": policy.config_hash.hex()}
                return await evaluate(
                    infra,
                    problems,
                    prover="symbolic",
                    policy=policy,
                    completions=None,
                    informal=informal,
                    samples=0,
                    concurrency=1,
                    manifest=manifest,
                )
            if not args.endpoint:
                raise EvalError("--endpoint is required for a model prover")
            spec = PROVERS[args.prover]
            model_policy = spec.policy()
            async with httpx.AsyncClient(base_url=args.endpoint) as http:
                service, model = await model_service(
                    spec, samples=samples, tokenizer_dir=args.tokenizer_dir, http=http
                )
                manifest |= {
                    "model": model,
                    "policy": {
                        "id": model_policy.id,
                        "config_hash": model_policy.config_hash.hex(),
                        "prompt_hashes": model_policy.prompt_hashes,
                        "prompt_layout": model_policy.prompt_format.layout.value,
                    },
                    "sampling_source": spec.sampling_source,
                    "published": {
                        "pass_at_32": spec.published_pass_at_32,
                        "source": spec.published_source,
                    },
                }
                return await evaluate(
                    infra,
                    problems,
                    prover=spec.key,
                    policy=model_policy,
                    completions=service,
                    informal=informal,
                    samples=samples,
                    concurrency=args.concurrency,
                    manifest=manifest,
                )

    report = asyncio.run(main())
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{report.prover}.json"
    path.write_text(json.dumps(report.to_json(), indent=1, ensure_ascii=False) + "\n")
    totals = report.totals()
    print(f"{report.prover}: {json.dumps(totals)}")
    print(f"wrote {path}")
    return 0


def _rank(args: argparse.Namespace) -> int:
    runs = {
        path.stem: ProverRun.from_json(json.loads(path.read_text()))
        for path in sorted(args.reports.glob("*.json"))
        if path.stem != "ranking"
    }
    models = {key: run for key, run in runs.items() if key in PROVERS}
    sample_counts = {run.samples_per_problem for run in models.values()}
    if len(models) < 2 or len(sample_counts) != 1:
        raise EvalError(
            f"a ranking needs at least two model reports at one sample count; "
            f"have { ({k: r.samples_per_problem for k, r in models.items()}) }"
        )
    (samples,) = sample_counts
    ranking = rank(models, {key: PROVERS[key].published_pass_at_32 for key in models})
    result: dict[str, Any] = {
        "samples_per_problem": samples,
        "pass_rates": {key: run.totals() for key, run in models.items()},
        "pairs": [
            {
                "higher": p.higher,
                "lower": p.lower,
                "published_gap_points": round(p.published_gap_points, 2),
                "measured_points": round(100 * p.measured.point, 2),
                "interval_points": [
                    round(100 * p.measured.low, 2),
                    round(100 * p.measured.high, 2),
                ],
                "problems": p.measured.n,
                "required": p.required,
                "verdict": p.verdict.value,
            }
            for p in ranking.pairs
        ],
        "ranking_reproduced": ranking.reproduced,
    }
    if args.absolute in models:
        if samples == 32:
            absolute = compare_absolute(
                models[args.absolute], PROVERS[args.absolute].published_pass_at_32
            )
            result["absolute"] = {
                "prover": args.absolute,
                **dataclasses.asdict(absolute),
                "gap_points": round(absolute.gap_points, 2),
                "within_tolerance": absolute.within_tolerance,
            }
        else:
            result["absolute"] = (
                f"not comparable: published numbers are pass@32, this is pass@{samples}"
            )
    if "symbolic" in runs and args.model in models:
        dom = dominance(models[args.model], runs["symbolic"])
        result["dominance"] = {
            "model": args.model,
            "strict": dom.strict,
            "missed": sorted(dom.missed),
            "gained": len(dom.gained),
        }
    (args.reports / "ranking.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rank Phase 3's provers on miniF2F (M3.12).")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run one prover (or `symbolic`) over a problem set")
    run.add_argument("--prover", required=True, choices=[*PROVERS, "symbolic"])
    run.add_argument("--endpoint", help="the vLLM serving that prover, e.g. http://127.0.0.1:8766")
    run.add_argument("--samples", type=int, default=32, help="samples per problem (pass@n)")
    run.add_argument("--split", default="test", choices=("test", "valid"))
    run.add_argument("--limit", type=int, help="a fixed-seed subset of the split, for a pilot")
    run.add_argument("--problems", help="comma-separated problem ids, instead of --split/--limit")
    run.add_argument("--seed", type=int, default=SEED)
    run.add_argument("--concurrency", type=int, default=4, help="attempts in flight at once")
    run.add_argument("--lean-concurrency", type=int, default=1)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--lake-project-dir", type=Path, default=_REPO / "packages" / "leankernel")
    run.add_argument(
        "--tokenizer-dir",
        type=Path,
        default=_REPO / "tests" / "models" / "data" / "tokenizers" / "Qwen3-0.6B",
    )
    run.add_argument("--bundle-root", type=Path, default=Path("/tmp/lean-agent-eval/bundles"))
    run.add_argument("--blob-root", type=Path, default=Path("/tmp/lean-agent-eval/blobs"))
    run.set_defaults(handler=_run)

    ranking = commands.add_parser("rank", help="compare the reports in a directory")
    ranking.add_argument("reports", type=Path)
    ranking.add_argument("--absolute", default="goedel-8b", help="the prover matched absolutely")
    ranking.add_argument("--model", default="goedel-8b", help="the model policy for dominance")
    ranking.set_defaults(handler=_rank)

    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
