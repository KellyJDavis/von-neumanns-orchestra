"""M3.1 -- `[models.<role>]` configuration and the completion types it fills in.

No infrastructure: these are types and a parser. What they are protecting is spec §6.5's promise
that "switching provers is one TOML line; a four-model ablation is four config files and no code",
which only holds if a typo in one of those lines is an error rather than a silently different
experiment.
"""

from __future__ import annotations

import dataclasses
import tomllib

import pytest
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.protocols import Completion, CompletionRequest, SamplingParams
from lean_agent_core.roles import ModelRole
from lean_agent_models.config import BackendConfig, ConfigError, parse_backend, parse_models

# Appendix B's own example, plus the `provenance` this repo requires (see `BackendConfig`).
APPENDIX_B = """
[models.prover]
backend = "vllm"
endpoint = "http://vllm:8000"
model_id = "Goedel-LM/Goedel-Prover-V2-8B"
tokenizer_revision = "abc123"
provenance = "open_weights"
sampling = { temperature = 0.8, top_p = 0.95, max_tokens = 40960, n = 8 }
context_tokens = 40960

[models.informal]
backend = "vllm"
model_id = "some/informal-model"
provenance = "open_weights"
"""


def _parse(text: str) -> dict[ModelRole, BackendConfig]:
    return parse_models(tomllib.loads(text))


def test_appendix_b_s_own_example_parses() -> None:
    models = _parse(APPENDIX_B)
    assert set(models) == {ModelRole.PROVER, ModelRole.INFORMAL}

    prover = models[ModelRole.PROVER]
    assert prover.model_id == "Goedel-LM/Goedel-Prover-V2-8B"
    assert prover.endpoint == "http://vllm:8000"
    assert prover.tokenizer_revision == "abc123"
    assert prover.provenance is ProvenanceClass.OPEN_WEIGHTS
    assert prover.sampling == SamplingParams(temperature=0.8, top_p=0.95, max_tokens=40960, n=8)
    assert prover.context_tokens == 40960
    assert prover.request_timeout_s is None

    # Every optional key really is optional, and the defaults are the documented ones.
    informal = models[ModelRole.INFORMAL]
    assert informal.context_tokens is None
    assert informal.endpoint is None
    assert informal.tokenizer_revision is None
    assert informal.seed is None
    assert informal.sampling == SamplingParams()


def test_switching_provers_is_one_line() -> None:
    """Spec's actual claim, as a test: change `model_id`, change nothing else."""
    before = _parse(APPENDIX_B)[ModelRole.PROVER]
    after = _parse(APPENDIX_B.replace("Goedel-LM/Goedel-Prover-V2-8B", "deepseek-ai/other"))[
        ModelRole.PROVER
    ]
    assert after.model_id == "deepseek-ai/other"
    assert (after.backend, after.endpoint, after.sampling) == (
        before.backend,
        before.endpoint,
        before.sampling,
    )


@pytest.mark.parametrize("key", ["backend", "model_id", "provenance"])
def test_a_missing_required_key_is_an_error_not_a_default(key: str) -> None:
    """A defaulted `model_id` would produce a run whose manifest names a model nobody chose."""
    raw = {"backend": "vllm", "model_id": "m", "provenance": "open_weights"}
    del raw[key]
    with pytest.raises(ConfigError, match=f"missing required key '{key}'"):
        parse_backend("prover", raw)


def test_an_unknown_key_is_rejected_rather_than_ignored() -> None:
    """The failure this prevents: `temprature = 0.9` silently dropped leaves the model sampling at
    0.0 while the config -- and therefore the run manifest -- says otherwise."""
    with pytest.raises(ConfigError, match=r"unknown sampling key\(s\): \['temprature'\]"):
        parse_backend(
            "prover",
            {
                "backend": "vllm",
                "model_id": "m",
                "provenance": "open_weights",
                "sampling": {"temprature": 0.9},
            },
        )
    with pytest.raises(ConfigError, match=r"unknown key\(s\): \['endpoin'\]"):
        parse_backend(
            "prover",
            {"backend": "vllm", "model_id": "m", "provenance": "open_weights", "endpoin": "x"},
        )


def test_an_unknown_role_names_the_ones_that_exist() -> None:
    with pytest.raises(ConfigError, match="unknown model role 'proovr'"):
        parse_backend("proovr", {"backend": "vllm", "model_id": "m", "provenance": "open_weights"})


def test_provenance_is_required_and_never_inferred_from_the_backend() -> None:
    """§7.1 turns the export rule on this value, and "served by vLLM, therefore open weights" is
    wrong in the direction that matters -- a closed model's weights serve under vLLM too."""
    closed = parse_backend(
        "critic",
        {"backend": "vllm", "model_id": "m", "provenance": "closed_api_eval_only"},
    )
    assert closed.provenance is ProvenanceClass.CLOSED_API_EVAL_ONLY

    with pytest.raises(ConfigError, match="provenance 'open-weights' is not one of"):
        parse_backend("prover", {"backend": "vllm", "model_id": "m", "provenance": "open-weights"})


def test_a_stop_string_is_rejected_because_toml_would_take_it_character_by_character() -> None:
    with pytest.raises(ConfigError, match="must be a list of strings"):
        parse_backend(
            "prover",
            {
                "backend": "vllm",
                "model_id": "m",
                "provenance": "open_weights",
                "sampling": {"stop": "\\n\\n"},
            },
        )


def test_sampling_canonical_form_is_stable_and_excludes_the_seed() -> None:
    """It is a cache-key component (§6.5: `sha256(prompt_tokens) ‖ model_id ‖ canonical(sampling)
    ‖ seed`) and the value stored on `trajectory.sampling`. The seed is named separately in that
    key, so folding it in here would make two runs differing only by seed share one cache entry."""
    params = SamplingParams(temperature=0.8, top_p=0.95, max_tokens=4096, n=8, stop=("\n\n",))
    canonical = params.canonical()
    assert canonical == {
        "temperature": 0.8,
        "top_p": 0.95,
        "max_tokens": 4096,
        "n": 8,
        "stop": ["\n\n"],
    }
    assert "seed" not in canonical
    assert (
        SamplingParams(temperature=0.8, top_p=0.95, max_tokens=4096, n=8).canonical() != canonical
    )


def test_a_completion_request_carries_token_ids_and_never_a_prompt_string() -> None:
    """The load-bearing decision in this layer (§6.5). A `str` prompt field would make the mistake
    available; there is deliberately no such field to set."""
    request = CompletionRequest(prompt_token_ids=(9707, 11, 1879))
    assert request.prompt_token_ids == (9707, 11, 1879)
    # There is no prompt-string field to reach for, which is the point -- checked against the
    # dataclass's declared fields rather than by `hasattr`, so a future `prompt: str` would fail
    # here even if it defaulted to None and left `hasattr` answering True.
    assert "prompt" not in {f.name for f in dataclasses.fields(CompletionRequest)}


def test_a_completion_requires_logprobs_parallel_to_its_tokens() -> None:
    """Non-optional by construction: §9 lists logprobs among the things that cannot be recomputed
    later, so a backend that cannot supply them has to fail at the boundary rather than write a
    NULL that surfaces at training time."""
    completion = Completion(
        token_ids=(13, 358), logprobs=(-1.44, -1.98), text=". I", finish_reason="length"
    )
    assert len(completion.logprobs) == len(completion.token_ids)
    with pytest.raises(TypeError):
        Completion(token_ids=(13,), text=".", finish_reason="length")  # type: ignore[call-arg]


_MINIMAL = {"backend": "vllm", "model_id": "m", "provenance": "open_weights"}


def test_a_configured_request_timeout_is_read_as_seconds() -> None:
    assert parse_backend("prover", {**_MINIMAL, "request_timeout_s": 90}).request_timeout_s == 90.0


@pytest.mark.parametrize("value", [0, -1, True, "40960", 40960.0])
def test_a_context_that_is_not_a_positive_integer_is_rejected(value: object) -> None:
    """M3.12. Every request's `max_tokens` is capped against this, so `context_tokens = true` or a
    quoted number is a typo that would otherwise cap every answer to one token or fail late."""
    with pytest.raises(ConfigError, match="context_tokens must be a positive integer"):
        parse_backend("prover", {**_MINIMAL, "context_tokens": value})


@pytest.mark.parametrize("value", [0, -5.0, False, "600"])
def test_a_timeout_that_is_not_a_positive_number_is_rejected(value: object) -> None:
    with pytest.raises(ConfigError, match="request_timeout_s must be a positive number"):
        parse_backend("prover", {**_MINIMAL, "request_timeout_s": value})


def test_top_k_is_read_and_left_unset_unless_stated() -> None:
    """M3.12. Unset, a request omits it and vLLM fills it from the model's `generation_config.json`
    (20 for every Phase 3 prover), so a configuration meaning "no top-k" has to say `top_k = 0`."""
    stated = parse_backend("prover", {**_MINIMAL, "sampling": {"top_k": 0}}).sampling
    assert stated.top_k == 0
    assert stated.canonical()["top_k"] == 0
    unstated = parse_backend("prover", {**_MINIMAL}).sampling
    assert unstated.top_k is None
    # Absent from the canonical form, so every cache key and trajectory written before it existed
    # still says exactly what it said.
    assert "top_k" not in unstated.canonical()


@pytest.mark.parametrize("value", [-1, True, "20", 2.5])
def test_a_top_k_vllm_would_not_accept_is_rejected(value: object) -> None:
    with pytest.raises(ConfigError, match="top_k must be 0"):
        parse_backend("prover", {**_MINIMAL, "sampling": {"top_k": value}})
