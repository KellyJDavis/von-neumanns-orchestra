"""M3.12 -- the prover-evaluation harness end to end, each prover replayed from a real recording.

Each of the three provers runs through `prover_eval`'s own `infrastructure`, `prepare` and `prove`
-- the real ingestion, materialization, control loop, executor, `/v1/check` and `/v1/link` against
a full-Mathlib base env -- on one miniF2F problem, with its completions replayed from a recording
of that prover served by a real vLLM. The replay server matches request bodies exactly, so a green
run is also evidence about what was *sent*: each prover's own published prompt layout with the
problem's informal statement, its own sampling with an explicit `top_k`, and `max_tokens` capped to
what the prompt leaves of the 40,960-token window. A byte of drift in any of them is a 409.

Re-record one prover against a live server -- the same test, in record mode, so the fixture holds
exactly what the harness sent:

    vllm serve <model> --port 8766 --max-model-len 40960 --generation-config vllm
    LEAN_AGENT_RECORD_PROVER=goedel-8b=http://127.0.0.1:8766 \\
        uv run pytest tests/eval/test_prover_eval_replay.py

The live run itself (hundreds of problems, many samples) is the manual CLI's job, never CI's.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from lean_agent_core.blobs import from_bytea
from lean_agent_core.codecs import decode_trajectory_token_ids
from lean_agent_eval.ranking import ProverRun
from lean_agent_eval.suites.minif2f import load_corpus, load_informal
from lean_agent_eval.suites.prover_eval import (
    CONTEXT_TOKENS,
    PROVERS,
    Infra,
    build_service,
    check_server,
    infrastructure,
    prepare,
    prove,
)
from lean_agent_models.template import load_chat_tokenizer
from sqlalchemy import Engine, text

# `replay_server` is a test helper that lives beside the model-layer tests; this suite runs in CI's
# `lean` job, so it is reached by path rather than duplicated (as `tests/leanserv` does).
sys.path.insert(0, str(Path(__file__).parents[1] / "models"))
from replay_server import RecordingTransport, create_replay_app, load_fixtures, write_fixtures

#: Outside the symbolic tail -- the null agent does not close it -- so a proof here is the model's.
PROBLEM = "imo_1959_p1"
SAMPLES = 2
RECORD_ENV = "LEAN_AGENT_RECORD_PROVER"
MODELS_DATA = Path(__file__).parents[1] / "models" / "data"
TOKENIZER_DIR = MODELS_DATA / "tokenizers" / "Qwen3-0.6B"


def fixture_path(key: str) -> Path:
    return MODELS_DATA / f"prover_eval_{key}.json"


def _recording() -> tuple[str, str] | None:
    value = os.environ.get(RECORD_ENV)
    if not value:
        return None
    key, separator, url = value.partition("=")
    if not separator or key not in PROVERS:
        raise AssertionError(
            f"{RECORD_ENV} must be <prover>=<url>, prover one of {sorted(PROVERS)}"
        )
    return key, url


@dataclass(frozen=True)
class Replayed:
    run: ProverRun
    #: The opening request's prompt, decoded from the ids the trajectory recorded (M3.11).
    prompt: str
    #: Every request body the recording holds -- which, replay being exact, is what this run sent.
    requests: list[dict[str, Any]]
    #: The trajectory's steps (M3.11): what was submitted and what Lean said about each.
    steps: list[dict[str, Any]]
    #: What the recording run concluded, from the fixture's provenance.
    expected_proved: bool


async def _trajectory(
    infra: Infra, run_id: uuid.UUID
) -> tuple[tuple[int, ...], list[dict[str, Any]]]:
    """The opening prompt's ids and the steps, as the one attempt's trajectory recorded them."""
    async with infra.sessions() as session:
        ids_blob, steps_blob = (
            await session.execute(
                text(
                    "SELECT t.token_ids_blob, t.steps_blob FROM trajectory t JOIN attempt a "
                    "ON a.id = t.attempt_id WHERE a.run_id = CAST(:r AS uuid)"
                ),
                {"r": str(run_id)},
            )
        ).one()
    exchanges = decode_trajectory_token_ids(await from_bytea(infra.blobs, bytes(ids_blob)))
    steps = json.loads(await from_bytea(infra.blobs, bytes(steps_blob)))
    return exchanges[0].prompt, list(steps)


@pytest.fixture(scope="module")
def replayed(
    mathlib: Path,
    admin_engine: Engine,
    app_async_database_url: str,
    leanserv_async_database_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Replayed | None]]:
    recording = _recording()
    problems = load_corpus().by_id([PROBLEM])
    informal = load_informal()
    tokenizer = load_chat_tokenizer(TOKENIZER_DIR)
    results: dict[str, Replayed | None] = {}
    run_ids: list[uuid.UUID] = []

    async def main() -> None:
        async with infrastructure(
            app_url=app_async_database_url,
            leanserv_url=leanserv_async_database_url,
            lake_project_dir=mathlib,
            bundle_root=tmp_path_factory.mktemp("prover_eval_bundles"),
            blob_root=tmp_path_factory.mktemp("prover_eval_blobs"),
            lean_concurrency=1,
            max_total_workers=1,
        ) as infra:
            # Every ingestion first, then every proof: one warm-up of each worker, not one per prover.
            prepared = {
                key: await prepare(infra, problems, policy_id="WholeProofSampler")
                for key in PROVERS
            }
            run_ids.extend(p.run_id for p in prepared.values())
            for key, spec in PROVERS.items():
                recorder: RecordingTransport | None = None
                if recording is not None and recording[0] == key:
                    async with httpx.AsyncClient(base_url=recording[1]) as plain:
                        serving_version = (await check_server(plain, spec.model_id))[
                            "serving_version"
                        ]
                    recorder = RecordingTransport(httpx.AsyncHTTPTransport())
                    http = httpx.AsyncClient(transport=recorder, base_url=recording[1])
                elif fixture_path(key).exists():
                    document = json.loads(fixture_path(key).read_text())
                    serving_version = document["provenance"]["serving_version"]
                    replay = create_replay_app(load_fixtures(fixture_path(key)))
                    http = httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=replay), base_url="http://replay"
                    )
                else:
                    results[key] = None
                    continue
                try:
                    service, _ = build_service(
                        spec,
                        samples=SAMPLES,
                        tokenizer_dir=TOKENIZER_DIR,
                        http=http,
                        serving_version=serving_version,
                    )
                    run = await prove(
                        infra,
                        prepared[key],
                        prover=key,
                        policy=spec.policy(),
                        completions=service,
                        informal=informal,
                        samples=SAMPLES,
                        concurrency=1,
                        manifest={},
                    )
                finally:
                    await http.aclose()
                if recorder is not None:
                    # Written before any assertion, so a recording that failed is still kept to read.
                    write_fixtures(
                        fixture_path(key),
                        provenance={
                            "recorded_from": f"{serving_version} (vllm-metal), Apple M2 Max",
                            "serving_version": serving_version,
                            "endpoint_path": "/v1/completions",
                            "model_id": spec.model_id,
                            "weights_revision": spec.weights_revision,
                            # What this recording concluded, so a replay can be held to it.
                            "outcome": {"problem": PROBLEM, "proved": run.outcomes[0].proved},
                            "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(
                                timespec="seconds"
                            ),
                            "note": (
                                "Recorded through RecordingTransport by "
                                "tests/eval/test_prover_eval_replay.py in record mode: "
                                f"{PROBLEM} under prover_eval's {key} spec, {SAMPLES} samples, "
                                f"base env {list(infra.base_env['imports'])}. Each request is "
                                "exactly what the harness built; each response is the server's."
                            ),
                        },
                        exchanges=recorder.exchanges,
                        name=f"{PROBLEM}_{key}",
                        note=f"prover_eval's {key} run on {PROBLEM}.",
                    )
                document = json.loads(fixture_path(key).read_text())
                prompt_ids, steps = await _trajectory(infra, prepared[key].run_id)
                results[key] = Replayed(
                    run=run,
                    prompt=tokenizer.decode(prompt_ids),
                    requests=[i["request"] for i in document["interactions"]],
                    steps=steps,
                    expected_proved=bool(document["provenance"]["outcome"]["proved"]),
                )

    asyncio.run(main())
    yield results
    with admin_engine.connect() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": run_id})
        conn.commit()


def _get(replayed: dict[str, Replayed | None], key: str) -> Replayed:
    result = replayed.get(key)
    assert result is not None, (
        f"no recording for {key} at {fixture_path(key)}; record it with "
        f"{RECORD_ENV}={key}=<url> (see this module's docstring)"
    )
    return result


@pytest.mark.parametrize("key", sorted(PROVERS))
def test_each_prover_runs_through_the_whole_harness(
    replayed: dict[str, Replayed | None], key: str
) -> None:
    """A test of the harness, not of the prover: replay must reach exactly the conclusion the
    recording did, proved or not. Requiring every prover to prove the problem would make CI a
    matter of sampling luck -- and a recorded miss is coverage too, of the path where Lean says no."""
    result = _get(replayed, key)
    run = result.run
    (outcome,) = run.outcomes
    assert outcome.problem_id == PROBLEM
    assert outcome.sealed and not outcome.infra_error
    assert outcome.samples == SAMPLES
    assert run.manifest["run_id"]
    # The obligation's own status, set only by `mark_proved` -- the whole acceptance path held.
    assert outcome.proved is result.expected_proved
    if not outcome.proved:
        # A miss has to be Lean's own: every submitted sample was refused with the checker's
        # diagnostics recorded, and nothing linked -- never a harness fault passing as a miss.
        submitted = [s for s in result.steps if str(s["action"]).startswith("SubmitProof")]
        assert submitted, "a miss with nothing submitted would be an extraction failure"
        assert not any(s["ok"] and s["action"] == "SubmitProof/link" for s in submitted)
        assert all(s["diagnostics"] for s in submitted if not s["ok"])


@pytest.mark.parametrize("key", sorted(PROVERS))
def test_each_prover_is_asked_in_its_own_published_shape(
    replayed: dict[str, Replayed | None], key: str
) -> None:
    result = _get(replayed, key)
    spec = PROVERS[key]
    informal = load_informal().for_problem(PROBLEM)
    assert f"/-- {informal}-/" in result.prompt, "the informal statement reached the prompt"
    marker = {
        "goedel-8b": ":= by sorry```",
        "pythagoras-4b": ":= by\n  sorry```",
        "kimina-distill-8b": f"# Problem:{informal}\n# Formal statement:",
    }[key]
    assert marker in result.prompt
    has_system = result.prompt.startswith("<|im_start|>system\nYou are an expert in mathematics")
    assert has_system is (key == "kimina-distill-8b")

    request = result.requests[0]
    assert (request["temperature"], request["top_p"], request["top_k"], request["n"]) == (
        spec.temperature,
        spec.top_p,
        spec.top_k,
        SAMPLES,
    )
    # The whole window, minus exactly what the prompt used (M3.12's cap).
    assert request["max_tokens"] == CONTEXT_TOKENS - len(request["prompt"])


def test_the_recordings_cover_a_proof(replayed: dict[str, Replayed | None]) -> None:
    """At least one recording proves the problem, so CI covers the path through `/v1/link` and
    `mark_proved`, not only the one where Lean refuses every sample."""
    proved = {key: r.run.outcomes[0].proved for key, r in replayed.items() if r is not None}
    assert any(proved.values()), f"no recording proves {PROBLEM}: {proved}"
