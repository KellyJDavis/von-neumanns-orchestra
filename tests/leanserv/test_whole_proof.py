"""M3.9 -- a language model proves a goal the null agent could not, through the whole pipeline.

Submission -> ingestion -> materialization -> claim -> `WholeProofSampler` -> the prover role ->
`/v1/check` screen -> `/v1/link` (kernel, replay, audit) -> `mark_proved`, against a real kernel and
a real PostgreSQL. The problem is miniF2F's `imo_1959_p1`, which M2.10's survey found the symbolic
portfolio does not close; the model is Goedel-Prover-V2-8B, the prover spec's Appendix B names.

**Two modes, one test.**

* *Replay* (the default, and what CI runs): the prover's responses are bytes recorded from a real
  vLLM serving Goedel-Prover-V2-8B, served by M3.2's replay app. Nothing about the policy, the
  tokenizer, the router, the client, leanserv or the kernel is replaced -- only token generation.
* *Record*: `LEAN_AGENT_RECORD_VLLM=http://127.0.0.1:8766 uv run pytest tests/leanserv/
  test_whole_proof.py` runs the identical pipeline against a live server and writes what crossed
  the wire to `tests/models/data/goedel_whole_proof.json` -- through `RecordingTransport`, so the
  fixture holds exactly the request this pipeline built. If any step that shapes the request moves
  (band 1's rendering, the prompt asset, the pinned tokenizer, the configured sampling, the
  client's body), replay's exact match turns that into a loud 409 rather than a stale pass.

**The base env is deliberately not full Mathlib**, for cost: measured, a full-Mathlib worker warms
in ~14 s at ~6 GiB and this pipeline needs two (sealing keys on the base env, linking on base env +
bundle), while the seven modules below warm in ~4 s at ~2 GiB and still define every namespace
Goedel's `open` line names. The prompt says exactly which modules those are, because band 1 is the
sealed goal *plus its base env* -- so the recording is of the model answering for this environment,
not for one it was not given.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from conftest import SealedObligation
from fastapi.testclient import TestClient
from lean_agent_api.ingestion import Ingestor, Submission
from lean_agent_api.materialize import BundleMaterializer
from lean_agent_core.actions import ObligationContext
from lean_agent_core.blobs import LocalBlobStore
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.executor import PolicyExecutor, TrajectoryWriter, load_context
from lean_agent_core.protocols import SamplingParams
from lean_agent_core.roles import ModelRole
from lean_agent_core.scheduler import ClaimedAttempt, claim_attempt
from lean_agent_core.state import ObligationOutcome, ObligationStateMachine
from lean_agent_eval.suites.minif2f import MEASURED_TAIL, load_corpus
from lean_agent_models.completions import RoutedCompletions
from lean_agent_models.config import BackendConfig
from lean_agent_models.router import ModelRouter, build_backend
from lean_agent_models.template import load_chat_tokenizer
from lean_agent_policies.whole_proof import WholeProofSampler
from lean_agent_serv.api import create_app
from lean_agent_serv.cache import VerificationCacheStore
from lean_agent_serv.pool import LeanReplPool, PoolConfig
from lean_agent_serv.verdicts import VerdictWriter
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_end_to_end import LeanServiceOverTestClient

# `replay_server` is a test helper that lives beside the model-layer tests; this suite runs in CI's
# `lean` job rather than the `python` job, so it is reached by path rather than duplicated.
sys.path.insert(0, str(Path(__file__).parents[1] / "models"))
from replay_server import (
    RecordingTransport,
    create_replay_app,
    load_fixtures,
    write_fixtures,
)

MODELS_DATA = Path(__file__).parents[1] / "models" / "data"
GOEDEL_FIXTURES = MODELS_DATA / "goedel_whole_proof.json"
RECORD_ENV = "LEAN_AGENT_RECORD_VLLM"

MODEL_ID = "Goedel-LM/Goedel-Prover-V2-8B"
#: The Hugging Face snapshot the recording was served from.
WEIGHTS_REVISION = "dfd02e6271a58375dfbf3ece0175277cf6b6a89a"
#: Goedel-Prover-V2-8B's tokenizer is byte-identical to Qwen3-0.6B's (see `record_templates.py`),
#: so the vendored converted Qwen3 tokenizer *is* this model's -- pinned by digest, which
#: `load_chat_tokenizer` verifies.
TOKENIZER_DIR = MODELS_DATA / "tokenizers" / "Qwen3-0.6B"
TOKENIZER_SHA256 = "41e00eccf531cffc2e562d38bdd879d41e5044ea279af5b73c6a32aabcc8fe04"

PROBLEM = "imo_1959_p1"
BASE_ENV_IMPORTS = (
    "Mathlib.Data.Real.Basic",
    "Mathlib.Topology.Defs.Filter",
    "Mathlib.Algebra.BigOperators.Group.Finset.Basic",
    "Mathlib.Data.Rat.Defs",
    "Mathlib.Tactic.Ring",
    "Mathlib.Tactic.Linarith",
    "Mathlib.Data.Nat.GCD.Basic",
)
#: Spec Appendix B's `[models.prover].sampling`, with `n = 4` rather than 8: the recording has to
#: be replayed on every CI run and stored in the repository, and four samples already carry the
#: point -- several independent attempts, each checked.
SAMPLING = SamplingParams(temperature=0.8, top_p=0.95, max_tokens=4096, n=4)
SEED = 1234


@pytest.fixture
def base_env(admin_engine: Engine) -> Iterator[str]:
    digest = f"whole-proof-{uuid.uuid4()}".encode()
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO base_env (digest, recipe, toolchain_rev, mathlib_rev) VALUES "
                "(:d, CAST(:recipe AS jsonb), 'v4.33.1', 'deadbeef')"
            ),
            {"d": digest, "recipe": json.dumps({"imports": list(BASE_ENV_IMPORTS)})},
        )
        conn.commit()
    yield digest.hex()
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM base_env WHERE digest = :d"), {"d": digest})
        conn.commit()


@pytest.fixture
def bundle_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("whole_proof_bundles")


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
    pool = LeanReplPool(lake_project_dir, PoolConfig(max_total_workers=2, bundle_root=bundle_root))
    app = create_app(
        pool,
        VerificationCacheStore(sessionmaker, blobs),
        VerdictWriter(sessionmaker, blobs),
        sessionmaker,
    )
    with TestClient(app) as client:
        yield client
    asyncio.run(engine.dispose())


def _model_http(endpoint: str | None) -> tuple[httpx.AsyncClient, RecordingTransport | None]:
    if endpoint is not None:
        recorder = RecordingTransport(httpx.AsyncHTTPTransport())
        return httpx.AsyncClient(transport=recorder, base_url=endpoint), recorder
    replay = create_replay_app(load_fixtures(GOEDEL_FIXTURES))
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=replay), base_url="http://replay"),
        None,
    )


def test_a_sampled_proof_closes_a_goal_the_null_agent_could_not(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    bundle_root: Path,
    lake_project_dir: Path,
    app_async_database_url: str,
    tmp_path: Path,
    sealed_obligation: SealedObligation,
) -> None:
    """`sealed_obligation` is a decoy: an open, claimable obligation belonging to another tenant,
    created before this test's own. `claim_attempt` takes the *oldest* open obligation in *any*
    running run, so an unscoped claim here takes the decoy -- verified by removing the scope below
    -- and the test then fails on another run's goal. The decoy keeps the scope from being dropped
    as unnecessary; the scope keeps the test from depending on nothing else being open.
    """
    # The premise, from M2.10's survey of all 488 problems: the portfolio does not close this one.
    assert PROBLEM not in MEASURED_TAIL

    (problem,) = load_corpus().by_id([PROBLEM])
    submission = Submission(
        base_env_digest=base_env,
        tenant_id=uuid.uuid4(),
        source=problem.as_sorry() + "\n",
        budget_attempts=1,
    )
    policy = WholeProofSampler()
    endpoint = os.environ.get(RECORD_ENV)
    run_ids: list[uuid.UUID] = []

    async def main() -> tuple[ClaimedAttempt, ObligationContext, ObligationOutcome, list[object]]:
        engine = create_async_engine(app_async_database_url)
        http, recorder = _model_http(endpoint)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            blobs = LocalBlobStore(tmp_path / "app_blobs")
            lean = LeanServiceOverTestClient(leanserv)

            ingested = await Ingestor(session_factory=sessions, lean=lean, blobs=blobs).ingest(
                submission
            )
            run_ids.append(ingested.run_id)
            await BundleMaterializer(
                session_factory=sessions,
                blobs=blobs,
                bundle_root=bundle_root,
                lake_project_dir=lake_project_dir,
            ).materialize_run(ingested.run_id)

            config = BackendConfig(
                role=ModelRole.PROVER,
                backend="vllm",
                model_id=MODEL_ID,
                provenance=ProvenanceClass.OPEN_WEIGHTS,
                endpoint=str(http.base_url),
                tokenizer_dir=TOKENIZER_DIR,
                tokenizer_revision=TOKENIZER_SHA256,
                weights_revision=WEIGHTS_REVISION,
                seed=SEED,
                sampling=SAMPLING,
            )
            service = RoutedCompletions(
                router=ModelRouter(
                    backends={ModelRole.PROVER: build_backend(config, client=http)},
                    configs={ModelRole.PROVER: config},
                ),
                tokenizers={
                    ModelRole.PROVER: load_chat_tokenizer(
                        TOKENIZER_DIR, expect_sha256=TOKENIZER_SHA256
                    )
                },
            )
            claimed = await claim_attempt(
                sessions,
                worker_id="whole-proof",
                policy_id=policy.id,
                policy_config_hash=policy.config_hash,
                # Scoped to this submission's tenant: spec §6.4's `$eligible_tenants`. The claim is
                # otherwise global by design (see this test's docstring and its decoy).
                eligible_tenants=[submission.tenant_id],
            )
            assert claimed is not None
            assert claimed.obligation_id != sealed_obligation.id
            assert claimed.run_id == ingested.run_id, "claimed another tenant's obligation"
            executor = PolicyExecutor(
                policy=policy,
                lean=lean,
                trajectories=TrajectoryWriter(sessions, blobs),
                # The shipped loader, not a hand-written one: its first real caller (see its
                # docstring for the two bugs that went unnoticed while it had none).
                context_loader=lambda o, r: load_context(sessions, o, r),
                completions=service,
            )
            ctx, _ = await load_context(sessions, claimed.obligation_id, claimed.run_id)
            outcome = (await executor.runner()(claimed)).outcome
            if outcome is ObligationOutcome.PROVED:
                await ObligationStateMachine(sessions).mark_proved(
                    claimed.obligation_id, claimed.attempt_id
                )
            return claimed, ctx, outcome, list(recorder.exchanges if recorder else [])
        finally:
            await http.aclose()
            await engine.dispose()

    try:
        claimed, ctx, outcome, exchanges = asyncio.run(main())
        if endpoint is not None:
            # Written before any assertion, so a recording whose samples all failed is still kept
            # and can be read; the assertions below then fail on it, loudly.
            write_fixtures(
                GOEDEL_FIXTURES,
                provenance={
                    "recorded_from": "vllm 0.28.0 (vllm-metal 0.28.0.dev20260910151954), Apple M2 Max",
                    "endpoint_path": "/v1/completions",
                    "model_id": MODEL_ID,
                    "weights_revision": WEIGHTS_REVISION,
                    "tokenizer_sha256": TOKENIZER_SHA256,
                    "license": (
                        "Goedel-Prover-V2-8B (the model) and Qwen3 (its tokenizer): Apache-2.0"
                    ),
                    "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(
                        timespec="seconds"
                    ),
                    "note": (
                        "Recorded through RecordingTransport by tests/leanserv/test_whole_proof.py "
                        f"in record mode: {PROBLEM}, base env {list(BASE_ENV_IMPORTS)}, sampling "
                        f"{SAMPLING.canonical()}, seed {SEED}. The request is exactly what the "
                        "pipeline built; the response is the server's, verbatim."
                    ),
                },
                exchanges=exchanges,  # type: ignore[arg-type]
                name=f"{PROBLEM}_whole_proof",
                note="WholeProofSampler's one request for this obligation, and vLLM's answer.",
            )
        _assert_proved(admin_engine, claimed, ctx, outcome, policy)
    finally:
        with admin_engine.connect() as conn:
            for run_id in run_ids:
                conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": run_id})
            conn.commit()


def _assert_proved(
    admin_engine: Engine,
    claimed: ClaimedAttempt,
    ctx: ObligationContext,
    outcome: ObligationOutcome,
    policy: WholeProofSampler,
) -> None:
    with admin_engine.connect() as conn:
        status, bundle_sha = conn.execute(
            text("SELECT status::text, bundle_sha FROM obligation WHERE id = :id"),
            {"id": claimed.obligation_id},
        ).one()
        verdict = conn.execute(
            text(
                "SELECT kind::text, link_ok, replay_ok, axiom_audit_ok FROM verdict "
                "WHERE attempt_id = :id"
            ),
            {"id": claimed.attempt_id},
        ).one()
        trajectory = conn.execute(
            text(
                "SELECT provenance::text, model_id, tokenizer_revision, "
                "sampling, seed, token_ids_blob IS NOT NULL, logprobs_blob IS NOT NULL, "
                "steps_blob FROM trajectory WHERE attempt_id = :id"
            ),
            {"id": claimed.attempt_id},
        ).one()

    # The shipped `load_context` reads what ingestion actually wrote.
    assert ctx.bundle_sha == bytes(bundle_sha).hex()
    assert ctx.base_env_imports == BASE_ENV_IMPORTS
    assert ctx.allow_sorry is False

    assert outcome is ObligationOutcome.PROVED
    assert status == "proved"
    assert verdict == ("proved", True, True, True)

    provenance, model_id, tokenizer, sampling, seed, has_ids, has_logprobs, _ = trajectory
    # §7.1: derived from the backend that served the completions, not asserted by the writer.
    assert provenance == ProvenanceClass.OPEN_WEIGHTS.value
    assert model_id == MODEL_ID
    assert tokenizer == TOKENIZER_SHA256
    # The *effective* sampling -- configured, since this policy overrides nothing -- not the
    # policy's empty override dict (see `CompletionResponse.sampling`).
    assert sampling == SAMPLING.canonical()
    assert seed == SEED
    assert has_ids and has_logprobs, "§9: token ids and logprobs cannot be recomputed later"
