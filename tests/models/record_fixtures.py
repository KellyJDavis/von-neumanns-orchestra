"""Record real vLLM responses into the fixtures `replay_server` serves.

Run manually against a real vLLM, never from CI:

    source ~/.venv-vllm-metal/bin/activate
    vllm serve Qwen/Qwen3-0.6B --port 8765 --max-model-len 2048
    uv run python tests/models/record_fixtures.py --endpoint http://127.0.0.1:8765

Why record rather than invent. CI has no GPU, so the model layer's tests need *some* stand-in --
and the obvious one, a hand-written server returning a plausible shape, tests the client against
whatever shape its author imagined. That is how a client ends up correctly parsing a response
nobody ever sends. Recording real bytes and replaying them verbatim keeps the schema under test
the one a real server actually emitted, while staying deterministic and offline. Same reasoning as
the vendored miniF2F corpus (M2.10), applied to a wire format instead of a benchmark.

What this cannot do is notice that vLLM's *next* version changed the shape. That is the manual
conformance run's job: re-record, and read the diff.
"""

from __future__ import annotations

import argparse
import datetime
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.protocols import CompletionRequest, SamplingParams
from lean_agent_core.roles import ModelRole
from lean_agent_models.client import CompletionsClient
from lean_agent_models.config import BackendConfig
from lean_agent_models.template import load_chat_tokenizer

FIXTURE_PATH = Path(__file__).parent / "data" / "vllm_completions.json"
TOKENIZER_DIR = Path(__file__).parent / "data" / "tokenizers" / "TinyLlama-1.1B-Chat-v1.0"

MODEL = "Qwen/Qwen3-0.6B"

#: "Hello, world" under Qwen3's tokenizer -- a fixed id list rather than text, because the whole
#: point of §6.5 is that this layer never sends a string. Recorded once so the fixtures do not
#: depend on a tokenizer being present to replay them.
HELLO_IDS = [9707, 11, 1879]

#: Each entry is `(name, note, CompletionRequest)`. The wire body is **built by the client
#: itself**, not written out here, so a fixture cannot describe a request the client would not
#: send. That matters because the replay server matches exactly: were these hand-written, the
#: fixtures could drift from `build_body` and the mismatch would show up as a 409 in a later
#: milestone rather than here, where a real server is available to re-record against.
SCENARIOS: list[tuple[str, str, CompletionRequest]] = [
    (
        "greedy_single",
        "The ordinary case: one deterministic sample with per-token logprobs.",
        CompletionRequest(
            prompt_token_ids=tuple(HELLO_IDS),
            sampling=SamplingParams(temperature=0.0, max_tokens=8),
            seed=1234,
        ),
    ),
    (
        "greedy_single_repeat",
        (
            "Byte-identical request to `greedy_single`, recorded separately so a test can assert "
            "the *server* was deterministic at record time rather than trusting the claim."
        ),
        CompletionRequest(
            prompt_token_ids=tuple(HELLO_IDS),
            sampling=SamplingParams(temperature=0.0, max_tokens=8),
            seed=1234,
        ),
    ),
    (
        "sampled_n4",
        "n>1: one request, several completions, which is how `WholeProofSampler` runs.",
        CompletionRequest(
            prompt_token_ids=tuple(HELLO_IDS),
            sampling=SamplingParams(temperature=0.8, top_p=0.95, max_tokens=12, n=4),
            seed=7,
        ),
    ),
    (
        "stop_string",
        "finish_reason='stop' rather than 'length' -- a different terminal branch.",
        CompletionRequest(
            prompt_token_ids=tuple(HELLO_IDS),
            sampling=SamplingParams(temperature=0.0, max_tokens=64, stop=(".",)),
            seed=1234,
        ),
    ),
    (
        "long_max_tokens",
        "A longer completion, so a test exercises more than a handful of logprobs.",
        CompletionRequest(
            prompt_token_ids=tuple(HELLO_IDS),
            sampling=SamplingParams(temperature=0.0, max_tokens=64),
            seed=1234,
        ),
    ),
    (
        "no_seed",
        (
            "No seed at all: the client omits the key rather than sending `null`, and a server may "
            "treat those differently."
        ),
        CompletionRequest(
            prompt_token_ids=tuple(HELLO_IDS),
            sampling=SamplingParams(temperature=0.0, max_tokens=8),
        ),
    ),
]

#: A request the server rejects, recorded so the client's error path is tested against real bytes.
UNKNOWN_MODEL = CompletionRequest(
    prompt_token_ids=tuple(HELLO_IDS),
    sampling=SamplingParams(temperature=0.0, max_tokens=4),
)


def _post(endpoint: str, body: dict[str, Any]) -> tuple[int, Any]:
    request = urllib.request.Request(
        f"{endpoint}/v1/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _synthesized(client: CompletionsClient) -> list[dict[str, Any]]:
    """Interactions no real vLLM will produce, marked as constructed rather than recorded.

    They model servers this project has actually met, or shapes a correct one cannot produce: Ollama's
    OpenAI-compatible layer accepts `logprobs` and returns a completion without them. Since §9
    lists logprobs among the things that cannot be recomputed later, the client must reject that
    rather than store a NULL -- and there is no way to record the case from a server that behaves
    correctly. Kept honest by the `recorded` flag, which a test asserts is false here and true
    everywhere else.
    """
    return [
        {
            "name": "missing_logprobs",
            "recorded": False,
            "note": (
                "Constructed, not recorded: a 200 response with no `logprobs` on the choice, "
                "which is what Ollama's OpenAI shim returns (ollama#16117). The client must raise "
                "`ModelProtocolError` rather than accept a completion it cannot store logprobs for."
            ),
            "request": client.build_body(
                CompletionRequest(
                    prompt_token_ids=tuple(HELLO_IDS),
                    sampling=SamplingParams(temperature=0.0, max_tokens=4),
                    seed=99,
                )
            ),
            "status": 200,
            "response": {
                "id": "cmpl-synthetic-missing-logprobs",
                "object": "text_completion",
                "created": 0,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "text": ". I am",
                        "logprobs": None,
                        "finish_reason": "length",
                        "stop_reason": None,
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            },
        },
        {
            "name": "logprobs_dropped",
            "recorded": False,
            "note": (
                "Constructed: `token_ids` present, `logprobs` null. Separated from "
                "`missing_logprobs` so the logprob check is isolated -- that fixture models "
                "Ollama, which returns neither, so the client trips on `token_ids` first and the "
                "logprob branch would never be reached."
            ),
            "request": client.build_body(
                CompletionRequest(
                    prompt_token_ids=tuple(HELLO_IDS),
                    sampling=SamplingParams(temperature=0.0, max_tokens=4),
                    seed=97,
                )
            ),
            "status": 200,
            "response": {
                "id": "cmpl-synthetic-logprobs-dropped",
                "object": "text_completion",
                "created": 0,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "text": ". I am",
                        "token_ids": [13, 358, 1079],
                        "logprobs": None,
                        "finish_reason": "length",
                        "stop_reason": None,
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6},
            },
        },
        {
            "name": "ragged_logprobs",
            "recorded": False,
            "note": (
                "Constructed: `token_logprobs` shorter than `tokens`. A client that zipped them "
                "would silently drop the tail rather than notice, so this pins the length check."
            ),
            "request": client.build_body(
                CompletionRequest(
                    prompt_token_ids=tuple(HELLO_IDS),
                    sampling=SamplingParams(temperature=0.0, max_tokens=4),
                    seed=98,
                )
            ),
            "status": 200,
            "response": {
                "id": "cmpl-synthetic-ragged",
                "object": "text_completion",
                "created": 0,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "text": ". I am",
                        "token_ids": [13, 358, 1079],
                        "logprobs": {
                            "tokens": [".", " I", " am"],
                            "token_logprobs": [-1.0, -2.0],
                            "top_logprobs": None,
                            "text_offset": None,
                        },
                        "finish_reason": "length",
                        "stop_reason": None,
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6},
            },
        },
    ]


def _client(model_id: str) -> CompletionsClient:
    """A client purely for its `build_body`. The tokenizer is the vendored TinyLlama one and is
    never consulted here -- prompts in these scenarios are already token ids, which is the whole
    point of §6.5 -- but `CompletionsClient` requires one, so it gets one."""
    return CompletionsClient(
        BackendConfig(
            role=ModelRole.PROVER,
            backend="vllm",
            model_id=model_id,
            provenance=ProvenanceClass.OPEN_WEIGHTS,
            endpoint="http://127.0.0.1:8765",
        ),
        load_chat_tokenizer(TOKENIZER_DIR),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765")
    parser.add_argument("--server-version", default="", help="recorded into provenance")
    args = parser.parse_args()

    client = _client(MODEL)
    interactions: list[dict[str, Any]] = []
    for name, note, request in SCENARIOS:
        body = client.build_body(request)
        status, response = _post(args.endpoint, body)
        print(f"  {name}: HTTP {status}")
        interactions.append(
            {
                "name": name,
                "recorded": True,
                "note": note,
                "request": body,
                "status": status,
                "response": response,
            }
        )

    unknown_body = _client("not/a-real-model").build_body(UNKNOWN_MODEL)
    status, response = _post(args.endpoint, unknown_body)
    print(f"  unknown_model: HTTP {status}")
    interactions.append(
        {
            "name": "unknown_model",
            "recorded": True,
            "note": "A real error response, so the client's error path is tested against real bytes.",
            "request": unknown_body,
            "status": status,
            "response": response,
        }
    )

    interactions.extend(_synthesized(client))

    document = {
        "provenance": {
            "recorded_from": args.server_version or "vllm (see --server-version)",
            "endpoint_path": "/v1/completions",
            "model_id": MODEL,
            "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "note": (
                "Real server responses, stored verbatim, keyed on request bodies built by "
                "`CompletionsClient.build_body` so they cannot drift from what the client sends. "
                "Interactions with recorded=false are constructed and say why in their own note."
            ),
        },
        "interactions": interactions,
    }
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(document, indent=1) + "\n")
    print(f"wrote {FIXTURE_PATH} ({len(interactions)} interactions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
