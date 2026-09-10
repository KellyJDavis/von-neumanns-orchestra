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

FIXTURE_PATH = Path(__file__).parent / "data" / "vllm_completions.json"

MODEL = "Qwen/Qwen3-0.6B"

#: "Hello, world" under Qwen3's tokenizer -- a fixed id list rather than text, because the whole
#: point of §6.5 is that this layer never sends a string. Recorded once so the fixtures do not
#: depend on a tokenizer being present to replay them.
HELLO_IDS = [9707, 11, 1879]

#: Each entry becomes one recorded interaction. `name` is what a test asks for; the request body is
#: sent to the real server exactly as written and stored exactly as sent.
REQUESTS: list[dict[str, Any]] = [
    {
        "name": "greedy_single",
        "note": "The ordinary case: one deterministic sample with per-token logprobs.",
        "body": {
            "model": MODEL,
            "prompt": HELLO_IDS,
            "max_tokens": 8,
            "temperature": 0.0,
            "seed": 1234,
            "logprobs": 1,
            "return_token_ids": True,
        },
    },
    {
        "name": "greedy_single_repeat",
        "note": (
            "Byte-identical request to `greedy_single`, recorded separately so a test can assert "
            "the *server* was deterministic at record time rather than trusting the claim."
        ),
        "body": {
            "model": MODEL,
            "prompt": HELLO_IDS,
            "max_tokens": 8,
            "temperature": 0.0,
            "seed": 1234,
            "logprobs": 1,
            "return_token_ids": True,
        },
    },
    {
        "name": "sampled_n4",
        "note": "n>1: one request, several completions, which is how `WholeProofSampler` runs.",
        "body": {
            "model": MODEL,
            "prompt": HELLO_IDS,
            "max_tokens": 12,
            "temperature": 0.8,
            "top_p": 0.95,
            "n": 4,
            "seed": 7,
            "logprobs": 1,
            "return_token_ids": True,
        },
    },
    {
        "name": "stop_string",
        "note": "finish_reason='stop' rather than 'length' -- a different terminal branch.",
        "body": {
            "model": MODEL,
            "prompt": HELLO_IDS,
            "max_tokens": 64,
            "temperature": 0.0,
            "seed": 1234,
            "stop": ["."],
            "logprobs": 1,
            "return_token_ids": True,
        },
    },
    {
        "name": "long_max_tokens",
        "note": "A longer completion, so a test exercises more than a handful of logprobs.",
        "body": {
            "model": MODEL,
            "prompt": HELLO_IDS,
            "max_tokens": 64,
            "temperature": 0.0,
            "seed": 1234,
            "logprobs": 1,
            "return_token_ids": True,
        },
    },
    {
        "name": "unknown_model",
        "note": "A real error response, so the client's error path is tested against real bytes.",
        "body": {
            "model": "not/a-real-model",
            "prompt": HELLO_IDS,
            "max_tokens": 4,
            "temperature": 0.0,
            "logprobs": 1,
        },
    },
]


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


def _synthesized() -> list[dict[str, Any]]:
    """Interactions no real vLLM will produce, marked as constructed rather than recorded.

    Only one so far, and it models a server this project has actually met: Ollama's
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
            "request": {
                "model": MODEL,
                "prompt": HELLO_IDS,
                "max_tokens": 4,
                "temperature": 0.0,
                "seed": 99,
                "logprobs": 1,
            },
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
            "name": "ragged_logprobs",
            "recorded": False,
            "note": (
                "Constructed: `token_logprobs` shorter than `tokens`. A client that zipped them "
                "would silently drop the tail rather than notice, so this pins the length check."
            ),
            "request": {
                "model": MODEL,
                "prompt": HELLO_IDS,
                "max_tokens": 4,
                "temperature": 0.0,
                "seed": 98,
                "logprobs": 1,
            },
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765")
    parser.add_argument("--server-version", default="", help="recorded into provenance")
    args = parser.parse_args()

    interactions: list[dict[str, Any]] = []
    for spec in REQUESTS:
        status, response = _post(args.endpoint, spec["body"])
        print(f"  {spec['name']}: HTTP {status}")
        interactions.append(
            {
                "name": spec["name"],
                "recorded": True,
                "note": spec["note"],
                "request": spec["body"],
                "status": status,
                "response": response,
            }
        )
    interactions.extend(_synthesized())

    document = {
        "provenance": {
            "recorded_from": args.server_version or "vllm (see --server-version)",
            "endpoint_path": "/v1/completions",
            "model_id": MODEL,
            "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "note": (
                "Real server responses, stored verbatim. Interactions with recorded=false are "
                "constructed and say why in their own note."
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
