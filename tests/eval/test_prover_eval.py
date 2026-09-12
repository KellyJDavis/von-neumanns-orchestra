"""M3.12 -- the prover-evaluation harness's own rules, without a model or a kernel.

The live run is manual (it needs a vLLM per prover and hours of generation); what is pinned here is
everything the harness decides on its own: that each prover is configured the way its published
numbers were produced, that a slow server is never scored as a prover's miss, and that a server
not serving the right model at the right window is refused before anything is spent.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

import httpx
import pytest
from lean_agent_core.scheduler import ClaimedAttempt
from lean_agent_core.state import ObligationOutcome
from lean_agent_core.worker import AttemptResult, InfraError
from lean_agent_eval.suites.prover_eval import (
    CONTEXT_TOKENS,
    DIVERGENCES,
    MAX_ENDPOINT_RETRIES,
    PROVERS,
    EvalError,
    check_server,
    endpoint_trouble_is_infra,
)
from lean_agent_models.errors import ModelProtocolError, ModelTimeout, ModelUnavailable
from lean_agent_policies.whole_proof import StatementLayout


def test_the_three_provers_are_the_ones_the_ranking_is_about() -> None:
    assert {spec.model_id for spec in PROVERS.values()} == {
        "Goedel-LM/Goedel-Prover-V2-8B",
        "Pythagoras-LM/Pythagoras-Prover-4B",
        "AI-MO/Kimina-Prover-Distill-8B",
    }
    for spec in PROVERS.values():
        assert re.fullmatch(r"[0-9a-f]{40}", spec.weights_revision), spec.key
        assert 0 < spec.published_pass_at_32 < 100 and spec.published_source


def test_every_prover_states_its_top_k_rather_than_inheriting_vllms_default() -> None:
    """Unset, vLLM fills `top_k` from the model's `generation_config.json` -- 20 for all three --
    and the trajectory never says so (M3.12). Each spec states it, from its own published code."""
    for spec in PROVERS.values():
        sampling = spec.sampling(32)
        assert sampling.top_k is not None and "top_k" in sampling.canonical(), spec.key
        assert (sampling.n, sampling.max_tokens) == (32, CONTEXT_TOKENS)
    assert (PROVERS["goedel-8b"].temperature, PROVERS["goedel-8b"].top_k) == (1.0, 0)
    assert PROVERS["pythagoras-4b"].top_k == 20
    assert PROVERS["kimina-distill-8b"].top_k == 0


def test_each_prover_gets_its_own_published_prompt_layout() -> None:
    layouts = {key: spec.policy().prompt_format.layout for key, spec in PROVERS.items()}
    assert layouts == {
        "goedel-8b": StatementLayout.GOEDEL_PIPELINE,
        "pythagoras-4b": StatementLayout.PYTHAGORAS_CARD,
        "kimina-distill-8b": StatementLayout.KIMINA_CARD,
    }


def test_the_divergences_are_stated_in_every_report() -> None:
    assert len(DIVERGENCES) == 5 and all(isinstance(d, str) and d for d in DIVERGENCES)


CLAIMED = ClaimedAttempt(attempt_id=uuid.uuid4(), obligation_id=uuid.uuid4(), run_id=uuid.uuid4())


def _failing(exc: Exception) -> Any:
    async def runner(claimed: ClaimedAttempt) -> AttemptResult:
        del claimed
        raise exc

    return runner


def test_a_slow_or_unreachable_server_is_infrastructure_until_retries_run_out() -> None:
    """Unbudgeted `InfraError`s first -- the obligation is reopened, not charged -- then a charged
    failure, so a dead server ends the run instead of looping on it."""

    async def main() -> AttemptResult:
        runner = endpoint_trouble_is_infra(_failing(ModelTimeout("no response within 600 s")))
        for _ in range(MAX_ENDPOINT_RETRIES):
            with pytest.raises(InfraError, match="ModelTimeout"):
                await runner(CLAIMED)
        result: AttemptResult = await runner(CLAIMED)
        return result

    result = asyncio.run(main())
    assert result.outcome is ObligationOutcome.RETRYABLE_FAILURE
    assert "endpoint trouble" in (result.detail or "")

    async def unavailable() -> None:
        with pytest.raises(InfraError, match="ModelUnavailable"):
            await endpoint_trouble_is_infra(_failing(ModelUnavailable("refused")))(CLAIMED)

    asyncio.run(unavailable())


def test_a_rejected_request_is_not_infrastructure() -> None:
    """A 4xx will be rejected identically however often it is sent (M3.4) -- a bug to charge, not
    a server to wait for."""

    async def main() -> None:
        with pytest.raises(ModelProtocolError):
            await endpoint_trouble_is_infra(_failing(ModelProtocolError("HTTP 400")))(CLAIMED)

    asyncio.run(main())


def _server(models: list[dict[str, Any]], version: str | None = "0.28.0") -> httpx.AsyncClient:
    """vLLM 0.28.0's own response shapes for `/v1/models` and `/version`."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": models})
        if request.url.path == "/version" and version is not None:
            return httpx.Response(200, json={"version": version})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://vllm")


def _check(client: httpx.AsyncClient, model_id: str) -> dict[str, Any]:
    async def main() -> dict[str, Any]:
        async with client:
            return await check_server(client, model_id)

    return asyncio.run(main())


GOEDEL = {"id": "Goedel-LM/Goedel-Prover-V2-8B", "object": "model", "max_model_len": 40960}


def test_the_server_must_serve_the_right_model_at_the_whole_window() -> None:
    served = _check(_server([GOEDEL]), GOEDEL["id"])
    assert served == {"max_model_len": 40960, "serving_version": "vllm==0.28.0"}
    with pytest.raises(EvalError, match="not AI-MO/Kimina-Prover-Distill-8B"):
        _check(_server([GOEDEL]), "AI-MO/Kimina-Prover-Distill-8B")
    with pytest.raises(EvalError, match="max_model_len=16384"):
        _check(_server([{**GOEDEL, "max_model_len": 16384}]), GOEDEL["id"])


def test_a_server_that_does_not_say_its_version_records_none_rather_than_a_guess() -> None:
    assert _check(_server([GOEDEL], version=None), GOEDEL["id"])["serving_version"] is None
