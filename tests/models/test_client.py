"""M3.4 -- `CompletionsClient` against the recorded vLLM responses.

Real client code, real HTTP, real bytes: `httpx.ASGITransport` routes into M3.2's replay server,
which returns responses a real vLLM 0.28.0 actually produced. Nothing here is a mock of the client,
and nothing here is an invented response shape.

The fixtures match requests **exactly**, so these tests also pin `build_body`: if the client's
request shape changes, every one of them fails with the replay server's 409 rather than passing
against an answer to a different question. `record_fixtures.py` builds its request bodies by calling
`build_body` itself, so the two cannot drift.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.protocols import CompletionRequest, CompletionResponse, SamplingParams
from lean_agent_core.roles import ModelRole
from lean_agent_models.client import CompletionsClient, default_timeout_s
from lean_agent_models.config import BackendConfig
from lean_agent_models.errors import ModelProtocolError, ModelUnavailable
from lean_agent_models.template import load_chat_tokenizer
from replay_server import Fixtures, create_replay_app, load_fixtures

DATA = Path(__file__).parent / "data"
TOKENIZER_DIR = DATA / "tokenizers" / "TinyLlama-1.1B-Chat-v1.0"


@pytest.fixture(scope="module")
def fixtures() -> Fixtures:
    return load_fixtures()


def _config(model_id: str = "Qwen/Qwen3-0.6B") -> BackendConfig:
    return BackendConfig(
        role=ModelRole.PROVER,
        backend="vllm",
        model_id=model_id,
        provenance=ProvenanceClass.OPEN_WEIGHTS,
        endpoint="http://replay",
    )


def _drive[T](
    fixtures: Fixtures, body: Callable[[CompletionsClient], Awaitable[T]], **kwargs: Any
) -> T:
    """Run one coroutine against a client wired to the replay app."""

    async def main() -> T:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_replay_app(fixtures)),
            base_url="http://replay",
        ) as http:
            client = CompletionsClient(
                kwargs.pop("config", None) or _config(),
                load_chat_tokenizer(TOKENIZER_DIR),
                client=http,
            )
            return await body(client)

    return asyncio.run(main())


def _complete(fixtures: Fixtures, request: CompletionRequest, **kwargs: Any) -> CompletionResponse:
    async def body(client: CompletionsClient) -> CompletionResponse:
        return await client.complete(request)

    return _drive(fixtures, body, **kwargs)


GREEDY = CompletionRequest(
    prompt_token_ids=(9707, 11, 1879),
    sampling=SamplingParams(temperature=0.0, max_tokens=8),
    seed=1234,
)


# --------------------------------------------------------------------------------------------
# The happy path, against real recorded bytes.
# --------------------------------------------------------------------------------------------


def test_a_completion_carries_token_ids_and_a_logprob_for_each(fixtures: Fixtures) -> None:
    response = _complete(fixtures, GREEDY)
    (completion,) = response.completions

    assert completion.token_ids
    assert len(completion.logprobs) == len(completion.token_ids)
    assert all(isinstance(value, float) for value in completion.logprobs)
    assert completion.finish_reason == "length"
    assert completion.text

    # Read from the recorded response rather than assumed to equal the request's, so a server that
    # re-tokenized the prompt would be visible instead of producing an unreplayable trajectory.
    assert response.prompt_token_ids == GREEDY.prompt_token_ids
    assert response.model_id == "Qwen/Qwen3-0.6B"
    assert response.tokenizer_revision is not None


def test_the_parsed_values_are_the_recorded_ones(fixtures: Fixtures) -> None:
    """Not merely well-formed: the same numbers the server actually returned."""
    recorded = fixtures.by_name("greedy_single").response["choices"][0]
    (completion,) = _complete(fixtures, GREEDY).completions

    assert list(completion.token_ids) == recorded["token_ids"]
    assert list(completion.logprobs) == recorded["logprobs"]["token_logprobs"]
    assert completion.text == recorded["text"]


def test_n_greater_than_one_yields_that_many_completions(fixtures: Fixtures) -> None:
    """How `WholeProofSampler` will run: one request, several independent samples."""
    response = _complete(
        fixtures,
        CompletionRequest(
            prompt_token_ids=(9707, 11, 1879),
            sampling=SamplingParams(temperature=0.8, top_p=0.95, max_tokens=12, n=4),
            seed=7,
        ),
    )
    assert len(response.completions) == 4
    assert len({c.token_ids for c in response.completions}) > 1
    for completion in response.completions:
        assert len(completion.logprobs) == len(completion.token_ids)


def test_a_stop_string_is_reported_as_a_stop_finish(fixtures: Fixtures) -> None:
    response = _complete(
        fixtures,
        CompletionRequest(
            prompt_token_ids=(9707, 11, 1879),
            sampling=SamplingParams(temperature=0.0, max_tokens=64, stop=(".",)),
            seed=1234,
        ),
    )
    assert response.completions[0].finish_reason == "stop"


# --------------------------------------------------------------------------------------------
# The request body, which the fixtures pin exactly.
# --------------------------------------------------------------------------------------------


def test_the_prompt_goes_out_as_token_ids(fixtures: Fixtures) -> None:
    """§6.5's whole point. The body carries integers under `prompt`, and there is no prompt string
    anywhere in it."""
    client = CompletionsClient(_config(), load_chat_tokenizer(TOKENIZER_DIR))
    body = client.build_body(GREEDY)

    assert body["prompt"] == [9707, 11, 1879]
    assert all(isinstance(token, int) for token in body["prompt"])
    assert body["return_token_ids"] is True
    assert body["logprobs"] == 0, "spec §6.5 wants the sampled token's logprob, not top-k"


def test_unset_seed_and_stop_are_omitted_rather_than_sent_empty(fixtures: Fixtures) -> None:
    """A server may treat an explicitly empty stop list, or a null seed, differently from an absent
    key. There is nothing to gain by finding out, and the recorded `no_seed` fixture pins it."""
    client = CompletionsClient(_config(), load_chat_tokenizer(TOKENIZER_DIR))
    body = client.build_body(
        CompletionRequest(
            prompt_token_ids=(9707, 11, 1879),
            sampling=SamplingParams(temperature=0.0, max_tokens=8),
        )
    )
    assert "seed" not in body
    assert "stop" not in body
    assert body == fixtures.by_name("no_seed").request


def test_a_config_seed_is_used_when_the_request_does_not_override_it() -> None:
    """`[models.<role>].seed` pins a whole deployment; a request may still override it."""
    config = BackendConfig(
        role=ModelRole.PROVER,
        backend="vllm",
        model_id="m",
        provenance=ProvenanceClass.OPEN_WEIGHTS,
        endpoint="http://replay",
        seed=4242,
    )
    client = CompletionsClient(config, load_chat_tokenizer(TOKENIZER_DIR))
    assert client.build_body(CompletionRequest(prompt_token_ids=(1,)))["seed"] == 4242
    assert client.build_body(CompletionRequest(prompt_token_ids=(1,), seed=7))["seed"] == 7


def test_every_recorded_request_is_one_this_client_would_send(fixtures: Fixtures) -> None:
    """The fixtures and `build_body` cannot drift, because the recorder calls `build_body`. This
    asserts that invariant from the other side: every recorded body has exactly the key set the
    client produces, so a fixture describing an impossible request would fail here."""
    client = CompletionsClient(_config(), load_chat_tokenizer(TOKENIZER_DIR))
    baseline = set(client.build_body(GREEDY))
    optional = {"stop", "seed"}

    for interaction in fixtures.interactions:
        keys = set(interaction.request)
        assert keys <= baseline | optional, interaction.name
        assert baseline - optional <= keys, interaction.name


# --------------------------------------------------------------------------------------------
# Everything the client refuses.
# --------------------------------------------------------------------------------------------


def test_the_ollama_shape_is_refused(fixtures: Fixtures) -> None:
    """A server that accepts the request and returns neither token ids nor logprobs. Refused on
    the first thing missing -- ids, without which the only way to get them is re-tokenizing the
    text, which is precisely what §6.5 forbids."""
    with pytest.raises(ModelProtocolError, match="no `token_ids`"):
        _complete(
            fixtures,
            CompletionRequest(
                prompt_token_ids=(9707, 11, 1879),
                sampling=SamplingParams(temperature=0.0, max_tokens=4),
                seed=99,
            ),
        )


def test_a_response_with_ids_but_no_logprobs_is_refused(fixtures: Fixtures) -> None:
    """Isolates the logprob check, which the fixture above cannot reach. Logprobs cannot be
    recomputed later (§9), so a completion missing them is unusable rather than degraded --
    accepting it would write a NULL that surfaces only when training needed behaviour-policy
    logprobs that no longer exist."""
    with pytest.raises(ModelProtocolError, match="without logprobs"):
        _complete(
            fixtures,
            CompletionRequest(
                prompt_token_ids=(9707, 11, 1879),
                sampling=SamplingParams(temperature=0.0, max_tokens=4),
                seed=97,
            ),
        )


def test_a_ragged_logprob_array_is_refused(fixtures: Fixtures) -> None:
    """Zipping them would silently drop the tail, leaving a trajectory that looks complete."""
    with pytest.raises(ModelProtocolError, match="logprobs for"):
        _complete(
            fixtures,
            CompletionRequest(
                prompt_token_ids=(9707, 11, 1879),
                sampling=SamplingParams(temperature=0.0, max_tokens=4),
                seed=98,
            ),
        )


def test_a_rejected_request_surfaces_as_a_protocol_error(fixtures: Fixtures) -> None:
    """A real recorded 404 from vLLM for an unknown model. A 4xx is the client's fault and will
    be rejected identically however many times it is sent, so it is not `ModelUnavailable`."""
    with pytest.raises(ModelProtocolError, match="rejected the request with HTTP 404"):
        _complete(
            fixtures,
            CompletionRequest(
                prompt_token_ids=(9707, 11, 1879),
                sampling=SamplingParams(temperature=0.0, max_tokens=4),
            ),
            config=_config("not/a-real-model"),
        )


def test_an_unreachable_endpoint_is_unavailable_not_a_protocol_error() -> None:
    """The distinction the control loop needs: infrastructure, not evidence about the content."""

    async def main() -> None:
        transport = httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused"))
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://nowhere") as http:
            client = CompletionsClient(_config(), load_chat_tokenizer(TOKENIZER_DIR), client=http)
            with pytest.raises(ModelUnavailable, match="unreachable"):
                await client.complete(GREEDY)

    asyncio.run(main())


def test_a_client_without_an_endpoint_or_a_transport_is_refused() -> None:
    config = BackendConfig(
        role=ModelRole.PROVER,
        backend="vllm",
        model_id="m",
        provenance=ProvenanceClass.OPEN_WEIGHTS,
    )
    with pytest.raises(ValueError, match="no `endpoint`"):
        CompletionsClient(config, load_chat_tokenizer(TOKENIZER_DIR))


def test_a_client_does_not_close_a_transport_it_was_given(fixtures: Fixtures) -> None:
    async def main() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_replay_app(fixtures)),
            base_url="http://replay",
        ) as http:
            async with CompletionsClient(
                _config(), load_chat_tokenizer(TOKENIZER_DIR), client=http
            ):
                pass
            assert http.is_closed is False

    asyncio.run(main())


# --------------------------------------------------------------------------------------------
# Provenance and tokenizing.
# --------------------------------------------------------------------------------------------


def test_provenance_comes_from_configuration_not_from_the_endpoint() -> None:
    """§7.1 derives `trajectory.provenance` from the backend at registration, and this is where
    that value lives. Two clients against the same endpoint can legitimately differ."""
    tokenizer = load_chat_tokenizer(TOKENIZER_DIR)
    open_weights = CompletionsClient(_config(), tokenizer)
    closed = CompletionsClient(
        BackendConfig(
            role=ModelRole.CRITIC,
            backend="vllm",
            model_id="m",
            provenance=ProvenanceClass.CLOSED_API_EVAL_ONLY,
            endpoint="http://replay",
        ),
        tokenizer,
    )
    assert open_weights.provenance is ProvenanceClass.OPEN_WEIGHTS
    assert closed.provenance is ProvenanceClass.CLOSED_API_EVAL_ONLY


def test_tokenize_is_local_and_needs_no_server() -> None:
    """Server-side templating is what §6.5 rules out, so this must not be a round trip -- asserted
    by having no transport at all."""

    async def main() -> tuple[int, ...]:
        client = CompletionsClient(_config(), load_chat_tokenizer(TOKENIZER_DIR))
        return await client.tokenize("<|user|>\nhi</s>\n")

    ids = asyncio.run(main())
    assert ids
    assert all(isinstance(token, int) for token in ids)


def test_the_timeout_follows_the_answer_it_allows_for() -> None:
    """M3.12. A non-streaming request is silent until it finishes, so its allowance has to cover
    decoding every token it may produce: at the provers' 40,960 that is about three hours, where a
    fixed 600 s would call a slow-but-succeeding answer an infrastructure failure. The request's
    own `timeout_ms` wins, then configuration, then the derivation."""
    derived = CompletionsClient(_config(), load_chat_tokenizer(TOKENIZER_DIR))
    long = CompletionRequest(prompt_token_ids=(1,), sampling=SamplingParams(max_tokens=40960))
    assert derived.timeout_s(long) == default_timeout_s(40960) == 600 + 40960 / 4
    assert derived.timeout_s(dataclasses.replace(long, timeout_ms=5000)) == 5.0

    configured = CompletionsClient(
        dataclasses.replace(_config(), request_timeout_s=90.0), load_chat_tokenizer(TOKENIZER_DIR)
    )
    assert configured.timeout_s(long) == 90.0
    assert configured.timeout_s(dataclasses.replace(long, timeout_ms=5000)) == 5.0


def test_the_request_on_the_wire_carries_the_derived_timeout(fixtures: Fixtures) -> None:
    """Not just computed: the HTTP request sent for a recorded completion carries it."""
    seen: list[dict[str, float]] = []

    class Watch(httpx.AsyncBaseTransport):
        def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
            self._inner = inner

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions["timeout"])
            return await self._inner.handle_async_request(request)

    async def main() -> None:
        replay = httpx.ASGITransport(app=create_replay_app(fixtures))
        async with httpx.AsyncClient(transport=Watch(replay), base_url="http://replay") as http:
            client = CompletionsClient(_config(), load_chat_tokenizer(TOKENIZER_DIR), client=http)
            await client.complete(GREEDY)

    asyncio.run(main())
    assert [(t["read"], t["connect"]) for t in seen] == [(default_timeout_s(8), 10.0)]
