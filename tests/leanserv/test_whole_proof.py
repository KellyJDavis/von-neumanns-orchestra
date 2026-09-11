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
import dataclasses
import datetime
import json
import os
import sys
import uuid
from collections.abc import Iterator
from html import escape
from pathlib import Path

import httpx
import pytest
from conftest import SealedObligation
from fastapi.testclient import TestClient
from lean_agent_api.app import create_app as create_public_app
from lean_agent_api.ingestion import Ingestor, Submission
from lean_agent_api.materialize import BundleMaterializer
from lean_agent_cli.client import ApiClient
from lean_agent_cli.main import main as cli_main
from lean_agent_core.actions import ObligationContext
from lean_agent_core.blobs import LocalBlobStore, from_bytea
from lean_agent_core.codecs import decode_trajectory_token_ids
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.executor import PolicyExecutor, TrajectoryWriter, load_context
from lean_agent_core.protocols import Policy, SamplingParams
from lean_agent_core.roles import ModelRole
from lean_agent_core.scheduler import ClaimedAttempt, claim_attempt
from lean_agent_core.state import ObligationOutcome, ObligationStateMachine
from lean_agent_eval.suites.minif2f import MEASURED_TAIL, load_corpus
from lean_agent_models.completions import RoutedCompletions
from lean_agent_models.config import BackendConfig
from lean_agent_models.router import ModelRouter, build_backend
from lean_agent_models.template import TokenizerRegistry, load_chat_tokenizer
from lean_agent_policies.repair import RepairLoop
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
#: Spec Appendix B's `[models.prover].sampling` as it stood when this was recorded (M3.12 raised
#: `max_tokens` to 40,960), with `n = 4` rather than 8: the recording has to be replayed on every
#: CI run and stored in the repository, and four samples already carry the point -- several
#: independent attempts, each checked.
SAMPLING = SamplingParams(temperature=0.8, top_p=0.95, max_tokens=4096, n=4)

#: The context both recordings were served at (`--max-model-len 16384`) and the repair prompt
#: budget that went with it -- three quarters, Goedel's pipeline's filter. M3.12 moved the defaults
#: to the provers' full 40,960; pinning the recorded values keeps the recordings meaning what they
#: recorded. Under them no prompt comes near the cap, so capping is exercised and changes nothing.
RECORDED_CONTEXT_TOKENS = 16_384
RECORDED_PROMPT_BUDGET_TOKENS = 12_288
SEED = 1234

#: M3.10. A problem whose first samples all fail and a repair succeeds, found by running the real
#: `RepairLoop` against the live prover over candidate problems (see CLAUDE.md's M3.10 notes).
REPAIR_FIXTURES = MODELS_DATA / "goedel_repair_loop.json"
REPAIR_PROBLEM = "mathd_algebra_209"
#: One first sample: the point is the repair, and every first sample that fails is one more
#: sequential repair chain to record and replay.
REPAIR_SAMPLING = dataclasses.replace(SAMPLING, n=1)
#: Chosen by trying seeds, the same way the problem was chosen, because most seeds never reach a
#: repair. Over seeds 1234 (two samples), 7, 42, 99, 314, 2024, 5 and 11, Goedel's first sample
#: proved this problem outright five times; seed 5's first repair ran out of tokens, seed 2024's two
#: repairs both failed, and seed 11's second repair linked -- the recording this replays. A seed is
#: a legitimate test input (the recording is still exactly what the real pipeline did with it), and
#: a lone seeded request replays deterministically, so the choice holds.
REPAIR_SEED = 11


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


def _model_http(
    endpoint: str | None, fixtures: Path
) -> tuple[httpx.AsyncClient, RecordingTransport | None]:
    if endpoint is not None:
        recorder = RecordingTransport(httpx.AsyncHTTPTransport())
        return httpx.AsyncClient(transport=recorder, base_url=endpoint), recorder
    replay = create_replay_app(load_fixtures(fixtures))
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=replay), base_url="http://replay"),
        None,
    )


@dataclasses.dataclass(frozen=True)
class Proved:
    claimed: ClaimedAttempt
    ctx: ObligationContext
    outcome: ObligationOutcome


@dataclasses.dataclass
class Pipeline:
    """The whole pipeline for one problem and one policy, in replay or record mode."""

    admin_engine: Engine
    base_env: str
    leanserv: TestClient
    bundle_root: Path
    lake_project_dir: Path
    app_async_database_url: str
    tmp_path: Path
    decoy: SealedObligation
    run_ids: list[uuid.UUID] = dataclasses.field(default_factory=list)

    @property
    def blobs(self) -> LocalBlobStore:
        return LocalBlobStore(self.tmp_path / "app_blobs")

    def prove(
        self,
        *,
        policy: Policy,
        problem_id: str,
        sampling: SamplingParams,
        fixtures: Path,
        note: str,
        seed: int = SEED,
    ) -> Proved:
        # The premise, from M2.10's survey of all 488 problems: the portfolio does not close it.
        assert problem_id not in MEASURED_TAIL
        (problem,) = load_corpus().by_id([problem_id])
        submission = Submission(
            base_env_digest=self.base_env,
            tenant_id=uuid.uuid4(),
            source=problem.as_sorry() + "\n",
            budget_attempts=1,
        )
        endpoint = os.environ.get(RECORD_ENV)

        async def main() -> tuple[Proved, list[object]]:
            engine = create_async_engine(self.app_async_database_url)
            http, recorder = _model_http(endpoint, fixtures)
            try:
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                lean = LeanServiceOverTestClient(self.leanserv)
                ingested = await Ingestor(
                    session_factory=sessions, lean=lean, blobs=self.blobs
                ).ingest(submission)
                self.run_ids.append(ingested.run_id)
                await BundleMaterializer(
                    session_factory=sessions,
                    blobs=self.blobs,
                    bundle_root=self.bundle_root,
                    lake_project_dir=self.lake_project_dir,
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
                    seed=seed,
                    sampling=sampling,
                    context_tokens=RECORDED_CONTEXT_TOKENS,
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
                    # Scoped to this submission's tenant: spec §6.4's `$eligible_tenants`. The
                    # claim is otherwise global by design -- see the decoy.
                    eligible_tenants=[submission.tenant_id],
                )
                assert claimed is not None
                assert claimed.obligation_id != self.decoy.id
                assert claimed.run_id == ingested.run_id, "claimed another tenant's obligation"
                executor = PolicyExecutor(
                    policy=policy,
                    lean=lean,
                    trajectories=TrajectoryWriter(sessions, self.blobs),
                    # The shipped loader, not a hand-written one (M3.9).
                    context_loader=lambda o, r: load_context(sessions, o, r),
                    completions=service,
                )
                ctx, _ = await load_context(sessions, claimed.obligation_id, claimed.run_id)
                outcome = (await executor.runner()(claimed)).outcome
                if outcome is ObligationOutcome.PROVED:
                    await ObligationStateMachine(sessions).mark_proved(
                        claimed.obligation_id, claimed.attempt_id
                    )
                return Proved(claimed, ctx, outcome), list(recorder.exchanges if recorder else [])
            finally:
                await http.aclose()
                await engine.dispose()

        proved, exchanges = asyncio.run(main())
        if endpoint is not None:
            # Written before any assertion, so a recording whose samples all failed is still kept
            # and can be read; the assertions then fail on it, loudly.
            write_fixtures(
                fixtures,
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
                        f"in record mode: {problem_id} under {policy.id}, base env "
                        f"{list(BASE_ENV_IMPORTS)}, sampling {sampling.canonical()}, seed {seed}. "
                        "Each request is exactly what the pipeline built; each response is the "
                        "server's, verbatim."
                    ),
                },
                exchanges=exchanges,  # type: ignore[arg-type]
                name=f"{problem_id}_{policy.id}",
                note=note,
            )
        return proved

    def cleanup(self) -> None:
        with self.admin_engine.connect() as conn:
            for run_id in self.run_ids:
                conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": run_id})
            conn.commit()


@pytest.fixture
def pipeline(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    bundle_root: Path,
    lake_project_dir: Path,
    app_async_database_url: str,
    tmp_path: Path,
    sealed_obligation: SealedObligation,
) -> Iterator[Pipeline]:
    """`sealed_obligation` is a decoy: an open, claimable obligation belonging to another tenant,
    created before this test's own. `claim_attempt` takes the *oldest* open obligation in *any*
    running run, so an unscoped claim takes the decoy -- verified by removing the scope -- and the
    test then fails on another run's goal. The decoy keeps the scope from being dropped as
    unnecessary; the scope keeps the test from depending on nothing else being open.
    """
    built = Pipeline(
        admin_engine,
        base_env,
        leanserv,
        bundle_root,
        lake_project_dir,
        app_async_database_url,
        tmp_path,
        sealed_obligation,
    )
    try:
        yield built
    finally:
        built.cleanup()


def test_a_sampled_proof_closes_a_goal_the_null_agent_could_not(pipeline: Pipeline) -> None:
    proved = pipeline.prove(
        policy=WholeProofSampler(),
        problem_id=PROBLEM,
        sampling=SAMPLING,
        fixtures=GOEDEL_FIXTURES,
        note="WholeProofSampler's one request for this obligation, and vLLM's answer.",
    )
    _assert_proved(pipeline.admin_engine, proved, SAMPLING, SEED)


def test_a_repair_closes_a_goal_the_first_samples_could_not(
    pipeline: Pipeline, capsys: pytest.CaptureFixture[str]
) -> None:
    """M3.10 end to end: every first sample fails `/v1/check`, the kernel's errors go back to the
    model in its trained repair format, and a repaired proof links, replays and audits."""
    proved = pipeline.prove(
        policy=RepairLoop(prompt_budget_tokens=RECORDED_PROMPT_BUDGET_TOKENS),
        problem_id=REPAIR_PROBLEM,
        sampling=REPAIR_SAMPLING,
        fixtures=REPAIR_FIXTURES,
        note="One of RepairLoop's requests for this obligation, and vLLM's answer.",
        seed=REPAIR_SEED,
    )
    _assert_proved(pipeline.admin_engine, proved, REPAIR_SAMPLING, REPAIR_SEED)

    with pipeline.admin_engine.connect() as conn:
        steps_blob, tokens_blob = conn.execute(
            text("SELECT steps_blob, token_ids_blob FROM trajectory WHERE attempt_id = :id"),
            {"id": proved.claimed.attempt_id},
        ).one()

    async def read() -> tuple[list[dict[str, object]], list[object]]:
        steps = json.loads(await from_bytea(pipeline.blobs, bytes(steps_blob)))
        exchanges = decode_trajectory_token_ids(
            await from_bytea(pipeline.blobs, bytes(tokens_blob))
        )
        return steps, list(exchanges)

    steps, exchanges = asyncio.run(read())
    submissions = [s for s in steps if str(s["action"]).startswith("SubmitProof")]
    winner = submissions[-1]
    assert winner["ok"] is True and winner["action"] == "SubmitProof"
    assert "repair" in str(winner["label"]), f"proved by a first sample: {winner['label']}"
    assert all(s["ok"] is False for s in submissions[:-1])

    opening, *repairs = exchanges
    assert repairs, "a repair is a second request, and the trajectory must record it separately"
    for repair in repairs:
        assert repair.sampling["n"] == 1  # type: ignore[attr-defined]
        # §6.6's byte-stability, observed in real token ids: a repair's conversation starts with
        # the opening prompt, so its ids begin with the opening prompt's ids -- which is what
        # lets a server's prefix cache reuse that prefill across rounds.
        prefix = opening.prompt  # type: ignore[attr-defined]
        assert repair.prompt[: len(prefix)] == prefix  # type: ignore[attr-defined]

    # M3.11: the viewer over this same attempt -- the richest trajectory there is, three requests
    # and three submissions, two of them failed with real kernel diagnostics.
    _assert_viewer_shows_exactly_what_was_sent(pipeline, proved, capsys)


def _assert_proved(
    admin_engine: Engine, proved: Proved, sampling: SamplingParams, seed: int
) -> None:
    claimed, ctx, outcome = proved.claimed, proved.ctx, proved.outcome
    # First, and with its own message. An attempt that proved nothing has no verdict row, so
    # querying for one fails with `NoResultFound` -- true, and useless as a diagnosis; two of the
    # seeds tried for the repair recording failed exactly that way before this line moved up.
    assert outcome is ObligationOutcome.PROVED, f"the attempt did not prove the goal: {outcome}"
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
                "sampling, seed, token_ids_blob IS NOT NULL, logprobs_blob IS NOT NULL "
                "FROM trajectory WHERE attempt_id = :id"
            ),
            {"id": claimed.attempt_id},
        ).one()

    # The shipped `load_context` reads what ingestion actually wrote.
    assert ctx.bundle_sha == bytes(bundle_sha).hex()
    assert ctx.base_env_imports == BASE_ENV_IMPORTS
    assert ctx.allow_sorry is False

    assert status == "proved"
    assert verdict == ("proved", True, True, True)

    provenance, model_id, tokenizer, recorded_sampling, recorded_seed, has_ids, has_logprobs = (
        trajectory
    )
    # §7.1: derived from the backend that served the completions, not asserted by the writer.
    assert provenance == ProvenanceClass.OPEN_WEIGHTS.value
    assert model_id == MODEL_ID
    assert tokenizer == TOKENIZER_SHA256
    # The *effective* sampling of the opening request -- configured, since neither policy
    # overrides it -- not the policy's empty override dict (see `CompletionResponse.sampling`).
    assert recorded_sampling == sampling.canonical()
    assert recorded_seed == seed
    assert has_ids and has_logprobs, "§9: token ids and logprobs cannot be recomputed later"


def _assert_viewer_shows_exactly_what_was_sent(
    pipeline: Pipeline, proved: Proved, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec §7.4's "exact rendered prompts (not reconstructions)", held to the wire.

    Three independent references, none of them the viewer's own code path: the ids the replay
    server received (the fixture's recorded requests), the chat template's own rendering of the
    policy's opening messages, and the server's own text for every sample. The viewer decodes
    stored ids; if that ever became a reconstruction -- or the stored ids stopped being the ones
    sent -- one of the three would disagree.
    """
    engine = create_async_engine(pipeline.app_async_database_url)
    app = create_public_app(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        lean=LeanServiceOverTestClient(pipeline.leanserv),
        blobs=pipeline.blobs,
        tokenizers=TokenizerRegistry.from_directories([TOKENIZER_DIR]),
    )
    attempt = proved.claimed.attempt_id
    try:
        with TestClient(app) as client:
            view = client.get(f"/v1/attempts/{attempt}/trajectory").json()
            page = client.get(f"/attempts/{attempt}")
            exit_code = cli_main(["trajectory", str(attempt)], client=ApiClient(client=client))
    finally:
        asyncio.run(engine.dispose())

    tokenizer = load_chat_tokenizer(TOKENIZER_DIR, expect_sha256=TOKENIZER_SHA256)
    recorded = load_fixtures(REPAIR_FIXTURES).interactions
    exchanges = view["exchanges"]
    assert len(exchanges) == len(recorded)

    opening = [
        {"role": m.role, "content": m.content} for m in RepairLoop().sampler.messages(proved.ctx)
    ]
    assert exchanges[0]["prompt"]["text"] == tokenizer.template.render(opening), (
        "the opening prompt, decoded from its stored ids, must be byte-identical to the render"
    )
    for exchange, interaction in zip(exchanges, recorded, strict=True):
        sent = interaction.request["prompt"]
        assert exchange["prompt"]["token_count"] == len(sent)
        assert exchange["prompt"]["text"] == tokenizer.decode(sent)
        assert exchange["prompt"]["decoded_with"] == TOKENIZER_SHA256
        for sample, choice in zip(
            exchange["completions"], interaction.response["choices"], strict=True
        ):
            assert sample["token_count"] == len(choice["token_ids"])
            assert sample["finish_reason"] == choice["finish_reason"]
            # The sample is the ids the server returned, decoded -- not the server's `text`,
            # which vLLM detokenizes with special tokens skipped and so drops the end-of-turn
            # token the model *did* generate (it is in `token_ids`, with a logprob). Found by this
            # assertion's first version, which compared against `text` and failed on exactly that
            # token after 5,040 identical characters. The difference is pinned to precisely it.
            assert sample["text"] == tokenizer.decode(choice["token_ids"])
            assert sample["text"].removesuffix("<|im_end|>") == choice["text"]

    requests_made = [s for s in view["steps"] if s["action"] == "RequestCompletion"]
    assert [s["exchange"] for s in requests_made] == list(range(len(recorded)))
    submissions = [s for s in view["steps"] if s["action"].startswith("SubmitProof")]
    for failed in submissions[:-1]:
        assert failed["ok"] is False and failed["diagnostics"], failed["label"]
        assert "theorem G_1" in failed["development"]
    assert view["verdict"]["proof"] == submissions[-1]["development"]

    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert escape("<|im_start|>user") in page.text
    assert "<|im_start|>" not in page.text, "a special token left unescaped is swallowed as a tag"

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "verdict: proved" in out and "=== prompt:" in out
