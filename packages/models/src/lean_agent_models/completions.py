"""The executor's `CompletionService`: a `RequestCompletion` turned into tokens.

This is where the model layer's pieces meet. A policy proposes chat turns for a *role*; the router
resolves the role to a backend, the backend's pinned tokenizer renders and tokenizes the turns
(§6.5's client-side templating), the cache is consulted where consulting it is sound, and what
comes back is what the executor records on the trajectory.

Deliberately not in `core`: `core` holds the executor and `models` depends on it, so the concrete
service lives here and `core` names only the protocol -- the same shape as `LeanService` and its
httpx implementation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from lean_agent_core.actions import RequestCompletion
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.executor import CompletionService
from lean_agent_core.protocols import CompletionRequest, CompletionResponse, SamplingParams
from lean_agent_core.roles import ModelRole

from lean_agent_models.cache import ResponseCacheStore, compute_response_cache_key, is_cacheable
from lean_agent_models.errors import ModelProtocolError
from lean_agent_models.router import ModelRouter
from lean_agent_models.template import ChatTokenizer


def fit_to_context(
    sampling: SamplingParams, prompt_tokens: int, context_tokens: int | None
) -> SamplingParams:
    """`sampling` with `max_tokens` capped to what a prompt leaves of the served context.

    Required, not cosmetic: vLLM 0.28.0 rejects a request whose prompt plus `max_tokens` exceeds
    its `--max-model-len` with a 400 ("This model's maximum context length is 40960 tokens.
    However, you requested 100 output tokens and your prompt contains 40900 input tokens ..."),
    verified against a live server. So a configuration asking for the whole window -- how
    Goedel-Prover-V2's own pipeline runs, `max_tokens = max_model_len` -- could not send a single
    request uncapped, and a repair after a 20,000-token prompt is sent 20,960. The capped value is
    what goes into the cache key and onto the trajectory, because it is what was asked.
    """
    if context_tokens is None:
        return sampling
    room = context_tokens - prompt_tokens
    if room < 1:
        # A 4xx's reasoning: this prompt is too long for this server however often it is sent.
        raise ModelProtocolError(
            f"a prompt of {prompt_tokens} tokens leaves no room for an answer in the served "
            f"context of {context_tokens} tokens"
        )
    return sampling if sampling.max_tokens <= room else replace(sampling, max_tokens=room)


@dataclass(frozen=True)
class RoutedCompletions:
    """`CompletionService` over a `ModelRouter`, optionally in front of a response cache.

    The tokenizers are held per role beside the router because rendering is this side's job and a
    `ModelBackend` does not expose its tokenizer -- `tokenize` gives ids for text, not a rendered
    chat prompt. Keeping them here means one place decides how a policy's turns become ids.
    """

    router: ModelRouter
    tokenizers: dict[ModelRole, ChatTokenizer]
    cache: ResponseCacheStore | None = None

    def provenance_for(self, role: ModelRole) -> ProvenanceClass:
        """§7.1: derived from the backend, never asserted by the caller."""
        return self.router.backend_for(role).provenance

    def _sampling(self, request: RequestCompletion) -> SamplingParams:
        """The role's configured sampling, with the policy's overrides applied.

        Configuration is the default and the policy narrows it, rather than the other way round:
        §6.5's promise is that an ablation is "four config files and no code", which fails the
        moment a policy's hardcoded temperature wins over the file.
        """
        configs = self.router.configs
        base = (
            configs[request.role].sampling
            if configs is not None and request.role in configs
            else SamplingParams()
        )
        if not request.sampling:
            return base
        overrides = dict(request.sampling)
        stop = overrides.get("stop", base.stop)
        return SamplingParams(
            temperature=float(overrides.get("temperature", base.temperature)),
            top_p=float(overrides.get("top_p", base.top_p)),
            max_tokens=int(overrides.get("max_tokens", base.max_tokens)),
            n=int(overrides.get("n", base.n)),
            stop=tuple(stop),
        )

    async def complete(self, request: RequestCompletion) -> CompletionResponse:
        backend = self.router.backend_for(request.role)
        tokenizer = self.tokenizers[request.role]
        sampling = self._sampling(request)

        configs = self.router.configs
        configured_seed = (
            configs[request.role].seed if configs is not None and request.role in configs else None
        )
        seed = request.seed if request.seed is not None else configured_seed

        prompt_token_ids = tokenizer.to_token_ids(
            [{"role": message.role, "content": message.content} for message in request.messages]
        )
        # Before the cache key: the capped value is what is sent, so it is what a hit must match.
        sampling = fit_to_context(
            sampling,
            len(prompt_token_ids),
            configs[request.role].context_tokens
            if configs is not None and request.role in configs
            else None,
        )

        cache_key = compute_response_cache_key(prompt_token_ids, backend.id, sampling, seed)
        cacheable = is_cacheable(sampling, seed)
        if self.cache is not None and cacheable:
            hit = await self.cache.get(cache_key)
            if hit is not None:
                return CompletionResponse(
                    completions=hit.completions,
                    prompt_token_ids=hit.prompt_token_ids,
                    model_id=hit.model_id,
                    model_weights_hash=hit.weights_revision,
                    tokenizer_revision=hit.tokenizer_revision,
                    cache_hit=True,
                    # The stored elapsed time is what the *original* call cost, which is what a
                    # budget report wants: a cached run that reported ~0 ms would understate what
                    # producing these tokens actually took.
                    elapsed_ms=hit.elapsed_ms,
                    # The cache key covers both, so these are exactly what the hit answered.
                    sampling=sampling,
                    seed=seed,
                )

        started = time.monotonic()
        response = await backend.complete(
            CompletionRequest(prompt_token_ids=prompt_token_ids, sampling=sampling, seed=seed)
        )
        response = CompletionResponse(
            completions=response.completions,
            prompt_token_ids=response.prompt_token_ids or prompt_token_ids,
            model_id=response.model_id,
            model_weights_hash=response.model_weights_hash,
            # Filled from the tokenizer that actually rendered this prompt. A backend cannot report
            # it -- the server never saw a tokenizer, only ids -- and without it the stored ids are
            # not interpretable back into text (§7.3).
            tokenizer_revision=response.tokenizer_revision or tokenizer.revision,
            cache_hit=False,
            elapsed_ms=response.elapsed_ms or int((time.monotonic() - started) * 1000),
            # The merged values this service actually sent, which a backend cannot report: it
            # never saw the configuration or the policy's overrides, only their result.
            sampling=sampling,
            seed=seed,
        )

        if self.cache is not None and cacheable:
            await self.cache.put(cache_key, response)
        return response


def _conforms_to_completion_service_protocol(
    router: ModelRouter, tokenizers: dict[ModelRole, ChatTokenizer]
) -> CompletionService:
    """Structural conformance under `mypy --strict`, the device `SymbolicPortfolio` uses for
    `Policy`: `core` names the protocol and cannot import this module, so without a typed use here
    the two would only meet at the executor's first `RequestCompletion`."""
    return RoutedCompletions(router=router, tokenizers=tokenizers)
