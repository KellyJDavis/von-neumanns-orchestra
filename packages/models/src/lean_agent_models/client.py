"""A typed `ModelBackend` over an OpenAI-compatible `/v1/completions` endpoint (spec §6.5).

Token ids in, token ids and per-token logprobs out. The wire shape was read off a real vLLM
0.28.0 rather than from the OpenAI schema, because the two fields this layer exists for are not in
that schema: `return_token_ids` (so the completion's ids come back rather than being re-derived by
tokenizing the text, which is exactly the re-tokenization §6.5 forbids) and `logprobs`.

**The response is validated, not trusted.** A backend that answers without logprobs, or with a
logprob array not parallel to its token ids, gets a `ModelProtocolError` rather than a degraded
`Completion`. That is not defensive programming for its own sake: §9 lists token ids and logprobs
among the things that cannot be recomputed correctly later, so accepting a response missing them
would write `trajectory.logprobs_blob` as NULL and the loss would surface only when training needed
behaviour-policy logprobs that no longer exist. Ollama's OpenAI-compatible layer does exactly this
-- accepts `logprobs`, returns none -- which is why the case has a recorded fixture.

Deliberately no retry, for the reason `LeanServiceClient` documents: a caller that knows whether a
*new sample* is the right answer is the one that should decide, and here that is the policy loop.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import Any, Self

import httpx
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.protocols import (
    Completion,
    CompletionRequest,
    CompletionResponse,
    ModelBackend,
)

from lean_agent_models.config import BackendConfig
from lean_agent_models.errors import ModelProtocolError, ModelTimeout, ModelUnavailable
from lean_agent_models.template import ChatTokenizer

#: Everything in a request that is not decoding: queueing behind other requests, prefilling a long
#: repair prompt, transferring the response. Prefilling 40,900 tokens alone took ~190 s for an 8B
#: prover on an M2 Max (M3.12). It was the whole default before M3.12, at 4,096 tokens -- and
#: marginal even then under load (M3.10).
BASE_TIMEOUT_S = 600.0

#: The decode rate, per sequence, a derived timeout allows for. Measured for an 8B prover on an
#: M2 Max: ~17 tok/s alone on a short prompt, ~5 per sequence with 24 sharing the server (M3.10),
#: and ~4 alone once the context nears the 40,960 window (M3.12). Decoding slows as the context
#: grows, so an answer running to the end of the window averages above its final rate, and 4
#: covers one sequence doing that. It does not cover `n = 8` all running long at once -- only 5.66
#: full-window sequences fit in that machine's KV cache -- and a deployment doing that sets
#: `request_timeout_s`. Erring long matters: giving up on a slow-but-succeeding request turns it
#: into an error the control loop treats as infrastructure.
MIN_DECODE_TOKENS_PER_S = 4.0

CONNECT_TIMEOUT_S = 10.0


def default_timeout_s(max_tokens: int) -> float:
    """A request's wallclock allowance when neither it nor its configuration names one.

    Derived from `max_tokens` rather than fixed, since a non-streaming request is silent until it
    finishes and the answer's length is what decides how long that is. At 40,960 tokens this is
    about three hours -- worth seeing before a laptop run, not a number to hide.
    """
    return BASE_TIMEOUT_S + max_tokens / MIN_DECODE_TOKENS_PER_S


#: Ask for the sampled token's logprob and nothing more. Spec §6.5 is explicit -- "Store the
#: sampled token's logprob as float32 ... **not top-k**" -- so requesting top-k would pay for
#: payload this system has already decided not to keep.
LOGPROBS_TOP_K = 0


class CompletionsClient:
    """`ModelBackend` over one OpenAI-compatible endpoint.

    Holds a `ChatTokenizer` because §6.5 puts templating on the client: `tokenize` is local, and
    the tokenizer's identity is part of what makes a trajectory replayable (§7.3 records
    `tokenizer_revision` beside the token ids it produced).
    """

    def __init__(
        self,
        config: BackendConfig,
        tokenizer: ChatTokenizer,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if client is None and not config.endpoint:
            raise ValueError(
                f"[models.{config.role}] has no `endpoint`, and no transport was supplied"
            )
        self._config = config
        self._tokenizer = tokenizer
        self._client = client or httpx.AsyncClient(
            base_url=config.endpoint or "",
            timeout=httpx.Timeout(BASE_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        )
        self._owned = client is None

    @property
    def id(self) -> str:
        return self._config.model_id

    @property
    def provenance(self) -> ProvenanceClass:
        """Declared in configuration, never inferred from the endpoint (§7.1)."""
        return self._config.provenance

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def tokenize(self, text: str) -> tuple[int, ...]:
        """Local, not a server round trip. Server-side templating is what §6.5 rules out."""
        return self._tokenizer.encode(text)

    def build_body(self, request: CompletionRequest) -> dict[str, Any]:
        """The exact JSON sent for `request`. Public because it is a contract, not an internal
        detail: `tests/models/data/vllm_completions.json` records real responses keyed on exactly
        these bodies, so a change here is a change to what has been verified against a real server.

        `seed` and `stop` are omitted when unset rather than sent as `null`/`[]` -- a server may
        treat an explicitly empty stop list differently from an absent one, and there is nothing to
        gain by finding out. Everything with a value is sent explicitly, including defaults, so the
        request says what it means rather than relying on server-side defaults agreeing with ours.

        `top_k` is omitted when unset too, and that one *does* leave a choice to the server: vLLM
        fills it from the model's `generation_config.json` (M3.12 -- 20 for every Phase 3 prover,
        recorded in the fixtures made before it). A configuration that cares states it.
        """
        sampling = request.sampling
        body: dict[str, Any] = {
            "model": self._config.model_id,
            "prompt": list(request.prompt_token_ids),
            "max_tokens": sampling.max_tokens,
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "n": sampling.n,
            "logprobs": LOGPROBS_TOP_K,
            "return_token_ids": True,
        }
        if sampling.stop:
            body["stop"] = list(sampling.stop)
        if sampling.top_k is not None:
            body["top_k"] = sampling.top_k
        seed = request.seed if request.seed is not None else self._config.seed
        if seed is not None:
            body["seed"] = seed
        return body

    def timeout_s(self, request: CompletionRequest) -> float:
        """The request's own `timeout_ms`, else the configured `request_timeout_s`, else one
        derived from the `max_tokens` actually being sent."""
        if request.timeout_ms is not None:
            return request.timeout_ms / 1000
        if self._config.request_timeout_s is not None:
            return self._config.request_timeout_s
        return default_timeout_s(request.sampling.max_tokens)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = self.build_body(request)
        timeout = httpx.Timeout(self.timeout_s(request), connect=CONNECT_TIMEOUT_S)

        started = time.monotonic()
        try:
            response = await self._client.post("/v1/completions", json=body, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ModelTimeout(f"no response within {timeout.read} s: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"{self._config.endpoint} unreachable: {exc}") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 500:
            # Separated from a 4xx because the responses differ: a server error may pass, while a
            # rejected request will be rejected identically however many times it is sent.
            raise ModelUnavailable(
                f"{self._config.model_id} returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        if response.status_code >= 400:
            raise ModelProtocolError(
                f"{self._config.model_id} rejected the request with HTTP "
                f"{response.status_code}: {response.text[:300]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelProtocolError(f"response was not JSON: {response.text[:300]}") from exc

        return self._parse(payload, elapsed_ms=elapsed_ms)

    def _parse(self, payload: dict[str, Any], *, elapsed_ms: int) -> CompletionResponse:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelProtocolError(f"response carried no choices: {payload!r:.300}")

        completions = tuple(
            self._parse_choice(choice, index) for index, choice in enumerate(choices)
        )
        first = choices[0]
        prompt_token_ids = first.get("prompt_token_ids")
        return CompletionResponse(
            completions=completions,
            # Echoed by the server rather than assumed equal to the request's, so a server that
            # re-tokenized is visible here instead of producing an unreplayable trajectory.
            prompt_token_ids=tuple(prompt_token_ids) if prompt_token_ids else (),
            model_id=str(payload.get("model", self._config.model_id)),
            tokenizer_revision=self._tokenizer.revision,
            elapsed_ms=elapsed_ms,
        )

    def _parse_choice(self, choice: dict[str, Any], index: int) -> Completion:
        token_ids = choice.get("token_ids")
        if not isinstance(token_ids, list):
            raise ModelProtocolError(
                f"choice {index} has no `token_ids`. The request asks for `return_token_ids`; a "
                "server that ignores it leaves only text, and re-tokenizing that is precisely the "
                "re-tokenization spec §6.5 forbids."
            )

        logprobs_block = choice.get("logprobs")
        token_logprobs = (
            logprobs_block.get("token_logprobs") if isinstance(logprobs_block, dict) else None
        )
        if not isinstance(token_logprobs, list):
            raise ModelProtocolError(
                f"choice {index} came back without logprobs. They cannot be recomputed later "
                "(spec §9), so a completion missing them is not a degraded success -- it is "
                "unusable. Some OpenAI-compatible servers accept `logprobs` and silently drop it."
            )
        if len(token_logprobs) != len(token_ids):
            raise ModelProtocolError(
                f"choice {index} has {len(token_logprobs)} logprobs for {len(token_ids)} tokens. "
                "Zipping them would silently discard the tail."
            )
        if any(value is None for value in token_logprobs):
            raise ModelProtocolError(
                f"choice {index} has a null logprob, so the stored array would not be a complete "
                "record of the sampled tokens."
            )

        return Completion(
            token_ids=tuple(int(token) for token in token_ids),
            logprobs=tuple(float(value) for value in token_logprobs),
            text=str(choice.get("text", "")),
            finish_reason=str(choice.get("finish_reason", "")),
        )


def _conforms_to_model_backend_protocol(
    config: BackendConfig, tokenizer: ChatTokenizer
) -> ModelBackend:
    """Structural conformance under `mypy --strict`, the device `SymbolicPortfolio` uses for
    `Policy`: without a typed use inside `packages/`, drift between the protocol and this class
    would surface only at the executor's first call."""
    return CompletionsClient(config, tokenizer)
