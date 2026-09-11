"""M3.7 -- `RoutedCompletions`, where the model layer's pieces meet.

A policy's chat turns go in; the router picks a backend, the pinned tokenizer renders and tokenizes
them (§6.5), the cache is consulted where that is sound, and token ids with logprobs come out. Real
client code over real HTTP into M3.2's replay server, so the bytes are ones a real vLLM produced.

No Postgres here: the cache is exercised against a real database in `tests/db/`, and what these
cover is the routing, the templating and the decision of *whether* to cache at all.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import httpx
import pytest
from lean_agent_core.actions import Message, RequestCompletion
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.protocols import CompletionResponse, SamplingParams
from lean_agent_core.roles import ModelRole
from lean_agent_models.cache import compute_response_cache_key
from lean_agent_models.completions import RoutedCompletions, fit_to_context
from lean_agent_models.config import BackendConfig
from lean_agent_models.errors import ModelProtocolError
from lean_agent_models.router import ModelRouter, build_backend
from lean_agent_models.template import load_chat_tokenizer, tokenizer_digest
from replay_server import Fixtures, create_replay_app, load_fixtures

DATA = Path(__file__).parent / "data"
TOKENIZER_DIR = DATA / "tokenizers" / "TinyLlama-1.1B-Chat-v1.0"

#: The prompt the M3.2 fixtures were recorded against. `RoutedCompletions` renders messages into
#: ids, so a test that wants a recorded response has to arrive at exactly these.
RECORDED_PROMPT = (9707, 11, 1879)


@pytest.fixture(scope="module")
def fixtures() -> Fixtures:
    return load_fixtures()


def _config(**overrides: Any) -> BackendConfig:
    return BackendConfig(
        role=ModelRole.PROVER,
        backend="vllm",
        model_id=overrides.pop("model_id", "Qwen/Qwen3-0.6B"),
        provenance=overrides.pop("provenance", ProvenanceClass.OPEN_WEIGHTS),
        endpoint="http://replay",
        tokenizer_dir=TOKENIZER_DIR,
        **overrides,
    )


def _service(
    fixtures: Fixtures, http: httpx.AsyncClient, config: BackendConfig | None = None
) -> RoutedCompletions:
    resolved = config or _config()
    router = ModelRouter(
        backends={ModelRole.PROVER: build_backend(resolved, client=http)},
        configs={ModelRole.PROVER: resolved},
    )
    return RoutedCompletions(
        router=router, tokenizers={ModelRole.PROVER: load_chat_tokenizer(TOKENIZER_DIR)}
    )


def _run(fixtures: Fixtures, body: Any, config: BackendConfig | None = None) -> Any:
    async def main() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_replay_app(fixtures)),
            base_url="http://replay",
        ) as http:
            return await body(_service(fixtures, http, config))

    return asyncio.run(main())


def test_messages_are_templated_and_tokenized_before_the_wire(fixtures: Fixtures) -> None:
    """§6.5's client-side templating, end to end: a policy proposes turns, and what leaves this
    process is a list of integers. The chat template is applied -- the ids are not just the raw
    content encoded -- which is what a chat model was trained to see."""
    tokenizer = load_chat_tokenizer(TOKENIZER_DIR)
    messages = [{"role": "user", "content": "Prove 2 + 2 = 4."}]

    templated = tokenizer.to_token_ids(messages)
    raw = tokenizer.encode("Prove 2 + 2 = 4.")

    assert templated != raw, "the template must actually be applied"
    assert len(templated) > len(raw), "a chat template adds turn markers"


def test_a_completion_comes_back_with_tokens_and_logprobs(fixtures: Fixtures) -> None:
    """Against a recorded vLLM response, reached through the whole stack."""

    async def body(service: RoutedCompletions) -> CompletionResponse:
        request = RequestCompletion(
            role=ModelRole.PROVER,
            messages=(Message(role="user", content="hello"),),
            sampling={"temperature": 0.0, "max_tokens": 8},
            seed=1234,
        )
        # Reach the recorded fixture by asking for exactly the prompt it was recorded with.
        service.tokenizers[ModelRole.PROVER] = _FixedTokenizer(RECORDED_PROMPT)  # type: ignore[assignment]
        return await service.complete(request)

    response = _run(fixtures, body)
    assert response.completions
    completion = response.completions[0]
    assert len(completion.logprobs) == len(completion.token_ids)
    assert response.prompt_token_ids == RECORDED_PROMPT
    assert response.cache_hit is False


def test_the_tokenizer_revision_is_filled_in_from_the_tokenizer_that_rendered_it(
    fixtures: Fixtures,
) -> None:
    """A backend cannot report it -- the server only ever saw ids -- and without it the stored ids
    are not interpretable back into text (§7.3)."""

    async def body(service: RoutedCompletions) -> CompletionResponse:
        service.tokenizers[ModelRole.PROVER] = _FixedTokenizer(RECORDED_PROMPT)  # type: ignore[assignment]
        return await service.complete(
            RequestCompletion(
                role=ModelRole.PROVER,
                messages=(Message(role="user", content="hello"),),
                sampling={"temperature": 0.0, "max_tokens": 8},
                seed=1234,
            )
        )

    response = _run(fixtures, body)
    # The genuine digest of the vendored converted tokenizer, not a value this test invented.
    assert response.tokenizer_revision == load_chat_tokenizer(TOKENIZER_DIR).revision
    assert response.tokenizer_revision == tokenizer_digest(TOKENIZER_DIR)


def test_provenance_is_read_off_the_backend(fixtures: Fixtures) -> None:
    """§7.1: never asserted by the caller."""

    async def body(service: RoutedCompletions) -> ProvenanceClass:
        return service.provenance_for(ModelRole.PROVER)

    assert _run(fixtures, body) is ProvenanceClass.OPEN_WEIGHTS
    assert (
        _run(fixtures, body, _config(provenance=ProvenanceClass.CLOSED_API_EVAL_ONLY))
        is ProvenanceClass.CLOSED_API_EVAL_ONLY
    )


def test_configured_sampling_is_the_default_and_the_policy_narrows_it(
    fixtures: Fixtures,
) -> None:
    """§6.5's promise is that an ablation is "four config files and no code", which fails the
    moment a policy's hardcoded temperature wins over the file. So configuration is the base and
    the policy overrides only what it names."""

    async def body(service: RoutedCompletions) -> tuple[SamplingParams, SamplingParams]:
        from_config = service._sampling(RequestCompletion(role=ModelRole.PROVER, messages=()))
        narrowed = service._sampling(
            RequestCompletion(role=ModelRole.PROVER, messages=(), sampling={"temperature": 0.1})
        )
        return from_config, narrowed

    config = _config(sampling=SamplingParams(temperature=0.8, top_p=0.95, max_tokens=4096, n=8))
    from_config, narrowed = _run(fixtures, body, config)

    assert from_config == SamplingParams(temperature=0.8, top_p=0.95, max_tokens=4096, n=8)
    # Only temperature moved; everything else is still the deployment's choice.
    assert narrowed == SamplingParams(temperature=0.1, top_p=0.95, max_tokens=4096, n=8)


def test_a_configured_seed_applies_and_a_request_may_override_it(fixtures: Fixtures) -> None:
    seen: list[int | None] = []

    class Recording(_FixedTokenizer):
        pass

    async def body(service: RoutedCompletions) -> None:
        service.tokenizers[ModelRole.PROVER] = Recording(RECORDED_PROMPT)  # type: ignore[assignment]
        backend = service.router.backend_for(ModelRole.PROVER)
        original = backend.complete

        async def spy(request: Any) -> Any:
            seen.append(request.seed)
            return await original(request)

        backend.complete = spy  # type: ignore[method-assign]
        await service.complete(
            RequestCompletion(
                role=ModelRole.PROVER,
                messages=(Message(role="user", content="hi"),),
                sampling={"temperature": 0.0, "max_tokens": 8},
            )
        )

    _run(fixtures, body, _config(seed=1234))
    assert seen == [1234], "the deployment's seed is used when the policy names none"


class _FixedTokenizer:
    """The **real** tokenizer, with only `to_token_ids` pinned to a fixed id list.

    Needed because the M3.2 fixtures were recorded against Qwen3's prompt while the vendored
    tokenizer is TinyLlama's, so rendering honestly would never land on a recorded response. Only
    the rendering is replaced: `revision` and `encode` are the genuine ones, so a test asserting
    the digest is asserting about a real vendored artifact rather than about this stub.
    """

    def __init__(self, ids: tuple[int, ...]) -> None:
        self._ids = ids
        self._real = load_chat_tokenizer(TOKENIZER_DIR)

    @property
    def revision(self) -> str | None:
        return self._real.revision

    def to_token_ids(self, messages: Any, **kwargs: Any) -> tuple[int, ...]:
        return self._ids

    def encode(self, text: str) -> tuple[int, ...]:
        return self._real.encode(text)


def test_the_response_reports_the_sampling_and_seed_that_were_actually_sent(
    fixtures: Fixtures,
) -> None:
    """A backend cannot say -- it sees only the merged request -- so the service that merged the
    deployment's configuration with the policy's overrides reports the result. That is what a
    trajectory must record (M3.9: the first real policy overrode nothing, and its trajectory said
    `sampling = {}`)."""

    async def body(service: RoutedCompletions) -> CompletionResponse:
        service.tokenizers[ModelRole.PROVER] = _FixedTokenizer(RECORDED_PROMPT)  # type: ignore[assignment]
        return await service.complete(
            RequestCompletion(
                role=ModelRole.PROVER,
                messages=(Message(role="user", content="hi"),),
                sampling={"temperature": 0.0, "max_tokens": 8},
            )
        )

    # The seed comes from configuration, the max_tokens from the policy: both are "what was sent".
    response = _run(fixtures, body, _config(seed=1234))
    assert response.sampling == SamplingParams(temperature=0.0, top_p=1.0, max_tokens=8, n=1)
    assert response.seed == 1234


def _greedy(max_tokens: int) -> RequestCompletion:
    """The recorded greedy request (temperature 0, seed 1234), asking for `max_tokens`."""
    return RequestCompletion(
        role=ModelRole.PROVER,
        messages=(Message(role="user", content="hello"),),
        sampling={"temperature": 0.0, "max_tokens": max_tokens},
        seed=1234,
    )


def test_max_tokens_is_capped_to_what_the_prompt_leaves_of_the_served_context(
    fixtures: Fixtures,
) -> None:
    """M3.12. vLLM rejects a request whose prompt plus `max_tokens` exceeds its window (HTTP 400,
    verified live), so asking for the whole window only works capped. With 11 tokens of context
    and a 3-token prompt, 64 becomes 8 --
    and the replay server, which matches bodies exactly, answers only because 8 is what went on
    the wire: the recorded request asked for 8."""

    async def body(service: RoutedCompletions) -> CompletionResponse:
        service.tokenizers[ModelRole.PROVER] = _FixedTokenizer(RECORDED_PROMPT)  # type: ignore[assignment]
        return await service.complete(_greedy(64))

    response = _run(fixtures, body, _config(context_tokens=len(RECORDED_PROMPT) + 8))
    assert response.sampling.max_tokens == 8, "the trajectory records what was sent"
    assert response.completions


def test_a_request_that_fits_the_served_context_is_sent_as_asked(fixtures: Fixtures) -> None:
    async def body(service: RoutedCompletions) -> CompletionResponse:
        service.tokenizers[ModelRole.PROVER] = _FixedTokenizer(RECORDED_PROMPT)  # type: ignore[assignment]
        return await service.complete(_greedy(8))

    assert _run(fixtures, body, _config(context_tokens=40960)).sampling.max_tokens == 8


def test_the_cache_is_keyed_on_the_capped_request(fixtures: Fixtures) -> None:
    """A hit has to answer the question that would have been sent, and that is the capped one."""
    keys: list[bytes] = []

    class _Store:
        async def get(self, key: bytes) -> None:
            keys.append(key)

        async def put(self, key: bytes, response: CompletionResponse) -> None:
            pass

    async def body(service: RoutedCompletions) -> CompletionResponse:
        cached = dataclasses.replace(service, cache=_Store())  # type: ignore[arg-type]
        cached.tokenizers[ModelRole.PROVER] = _FixedTokenizer(RECORDED_PROMPT)  # type: ignore[assignment]
        return await cached.complete(_greedy(64))

    _run(fixtures, body, _config(context_tokens=len(RECORDED_PROMPT) + 8))
    capped = SamplingParams(temperature=0.0, max_tokens=8)
    assert keys == [compute_response_cache_key(RECORDED_PROMPT, "Qwen/Qwen3-0.6B", capped, 1234)]


def test_a_prompt_that_fills_the_served_context_is_refused_before_anything_is_sent(
    fixtures: Fixtures,
) -> None:
    """The server would reject it however often it was sent, so it is not sent at all."""
    sent: list[object] = []

    async def body(service: RoutedCompletions) -> CompletionResponse:
        service.tokenizers[ModelRole.PROVER] = _FixedTokenizer(RECORDED_PROMPT)  # type: ignore[assignment]
        backend = service.router.backend_for(ModelRole.PROVER)
        original = backend.complete

        async def spy(request: Any) -> Any:
            sent.append(request)
            return await original(request)

        backend.complete = spy  # type: ignore[method-assign]
        return await service.complete(_greedy(8))

    with pytest.raises(ModelProtocolError, match="leaves no room for an answer"):
        _run(fixtures, body, _config(context_tokens=len(RECORDED_PROMPT)))
    assert sent == []


def test_fit_to_context_caps_and_never_raises_the_limit() -> None:
    wide = SamplingParams(max_tokens=40960)
    assert fit_to_context(wide, 500, None) is wide, "no declared context, nothing capped"
    assert fit_to_context(wide, 500, 40960).max_tokens == 40460
    assert fit_to_context(wide, 40959, 40960).max_tokens == 1
    narrow = SamplingParams(max_tokens=100)
    assert fit_to_context(narrow, 500, 40960) is narrow, "a cap, never a raise"
    with pytest.raises(ModelProtocolError):
        fit_to_context(wide, 40960, 40960)
