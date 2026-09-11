"""M3.5 -- `ModelRouter`, role to backend.

Spec §6.5's claim for this layer is concrete and testable: *"Switching provers is one TOML line; a
four-model ablation is four config files and no code."* Most of what follows is that claim, plus
the two things the router enforces so nobody else has to -- that a policy's declared roles are all
configured, and that a run can be asked what provenances it is about to mix before it mixes them.
"""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from typing import Any

import pytest
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.protocols import CompletionRequest, CompletionResponse, ModelBackend
from lean_agent_core.roles import ModelRole
from lean_agent_models.client import CompletionsClient
from lean_agent_models.config import BackendConfig, ConfigError, parse_models
from lean_agent_models.router import ModelRouter, RoleNotConfigured, build_backend
from lean_agent_policies.symbolic import SymbolicPortfolio

TOKENIZER_DIR = Path(__file__).parent / "data" / "tokenizers" / "TinyLlama-1.1B-Chat-v1.0"

CONFIG = f"""
[models.prover]
backend = "vllm"
endpoint = "http://vllm:8000"
model_id = "Goedel-LM/Goedel-Prover-V2-8B"
provenance = "open_weights"
tokenizer_dir = "{TOKENIZER_DIR}"
weights_revision = "abc123"
serving_version = "vllm==0.28.0"
seed = 1234
sampling = {{ temperature = 0.8, top_p = 0.95, max_tokens = 4096, n = 8 }}
context_tokens = 40960

[models.decomposer]
backend = "vllm"
endpoint = "http://vllm:8001"
model_id = "some/decomposer"
provenance = "open_weights"
tokenizer_dir = "{TOKENIZER_DIR}"
"""


class FakeBackend:
    """A `ModelBackend` with no transport, for the cases that are about routing rather than HTTP."""

    def __init__(self, model_id: str, provenance: ProvenanceClass) -> None:
        self._id = model_id
        self._provenance = provenance

    @property
    def id(self) -> str:
        return self._id

    @property
    def provenance(self) -> ProvenanceClass:
        return self._provenance

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise NotImplementedError

    async def tokenize(self, text: str) -> tuple[int, ...]:
        return ()


def _models(text: str = CONFIG) -> dict[ModelRole, BackendConfig]:
    return parse_models(tomllib.loads(text))


def _router(**backends: ModelBackend) -> ModelRouter:
    return ModelRouter(backends={ModelRole(role): b for role, b in backends.items()})


# --------------------------------------------------------------------------------------------
# The indirection spec asks for.
# --------------------------------------------------------------------------------------------


def test_a_policy_asks_for_a_role_and_gets_whatever_serves_it() -> None:
    router = ModelRouter.from_config(_models())

    prover = router.backend_for(ModelRole.PROVER)
    assert prover.id == "Goedel-LM/Goedel-Prover-V2-8B"
    assert router.backend_for(ModelRole.DECOMPOSER).id == "some/decomposer"
    assert router.roles == {ModelRole.PROVER, ModelRole.DECOMPOSER}


def test_switching_provers_is_one_toml_line_and_no_code() -> None:
    """Spec's claim, exactly as written. The same code, the same call, a different model."""
    before = ModelRouter.from_config(_models())
    after = ModelRouter.from_config(
        _models(CONFIG.replace("Goedel-LM/Goedel-Prover-V2-8B", "deepseek-ai/other"))
    )

    assert before.backend_for(ModelRole.PROVER).id == "Goedel-LM/Goedel-Prover-V2-8B"
    assert after.backend_for(ModelRole.PROVER).id == "deepseek-ai/other"
    assert before.roles == after.roles


def test_an_unconfigured_role_names_the_ones_that_exist() -> None:
    router = ModelRouter.from_config(_models())
    with pytest.raises(RoleNotConfigured, match=r"no backend for role 'critic'"):
        router.backend_for(ModelRole.CRITIC)


# --------------------------------------------------------------------------------------------
# What the router checks before a run starts.
# --------------------------------------------------------------------------------------------


def test_a_policys_roles_are_checked_up_front_not_at_first_call() -> None:
    """`Policy.roles` exists so this is answerable before anything is spent. Discovering a missing
    `decomposer` at the moment a policy first asks for one wastes the whole run up to that point
    and reports a configuration mistake as a mid-run failure."""
    router = _router(prover=FakeBackend("m", ProvenanceClass.OPEN_WEIGHTS))

    router.require(frozenset({ModelRole.PROVER}))
    with pytest.raises(RoleNotConfigured, match=r"needs role\(s\) \['critic', 'decomposer'\]"):
        router.require(frozenset({ModelRole.PROVER, ModelRole.CRITIC, ModelRole.DECOMPOSER}))


def test_the_symbolic_portfolio_needs_no_backend_at_all() -> None:
    """The same fact that makes Phase 2's "zero model calls" checkable rather than asserted: a
    policy declaring no roles passes against an empty router."""
    empty = ModelRouter(backends={})
    empty.require(SymbolicPortfolio().roles)
    assert SymbolicPortfolio().roles == frozenset()
    assert empty.roles == frozenset()


def test_a_bad_tokenizer_pin_fails_at_build_not_at_first_completion() -> None:
    """Eager construction. A run that would fail on its first `prover` call should fail before it
    claims any work, and the digest check (M3.4) is part of what is verified then."""
    text = CONFIG.replace(
        '[models.prover]\nbackend = "vllm"',
        '[models.prover]\ntokenizer_revision = "' + "0" * 64 + '"\nbackend = "vllm"',
    )
    with pytest.raises(ConfigError, match="expected " + "0" * 64):
        ModelRouter.from_config(_models(text))


def test_a_backend_without_a_tokenizer_directory_is_refused() -> None:
    """§6.5 renders the chat template client-side, so a model whose tokenizer this system does not
    have cannot be prompted at all -- said at build time rather than discovered later."""
    text = CONFIG.replace(
        f'tokenizer_dir = "{TOKENIZER_DIR}"\nweights_revision', "weights_revision"
    )
    with pytest.raises(ConfigError, match="has no `tokenizer_dir`"):
        ModelRouter.from_config(_models(text))


# --------------------------------------------------------------------------------------------
# Provenance and the manifest.
# --------------------------------------------------------------------------------------------


def test_a_mixed_provenance_run_is_visible_before_it_runs() -> None:
    """§7.1's exporter "raises (does not filter)" on anything outside
    `{open_weights, symbolic, human}`. Finding that out after a corpus has been collected is
    finding out too late, and the router is where a run can be asked in advance."""
    mixed = _router(
        prover=FakeBackend("open", ProvenanceClass.OPEN_WEIGHTS),
        critic=FakeBackend("closed", ProvenanceClass.CLOSED_API_EVAL_ONLY),
    )
    assert mixed.provenances() == {
        ProvenanceClass.OPEN_WEIGHTS,
        ProvenanceClass.CLOSED_API_EVAL_ONLY,
    }

    exportable = {ProvenanceClass.OPEN_WEIGHTS, ProvenanceClass.SYMBOLIC, ProvenanceClass.HUMAN}
    assert not mixed.provenances() <= exportable
    assert ModelRouter.from_config(_models()).provenances() <= exportable


def test_the_manifest_entry_carries_what_a_published_result_must_name() -> None:
    """§7.3's `models` array. `weights_revision` and `serving_version` are configuration rather
    than anything derivable from a model id: the same id serves different weights across
    revisions, and the same weights give different token ids across serving versions."""
    entries = ModelRouter.from_config(_models()).manifest_entries()

    assert [entry["role"] for entry in entries] == ["decomposer", "prover"], "sorted, so stable"
    prover = next(entry for entry in entries if entry["role"] == "prover")
    assert prover == {
        "role": "prover",
        "backend": "vllm",
        "model_id": "Goedel-LM/Goedel-Prover-V2-8B",
        "weights_revision": "abc123",
        "tokenizer_revision": None,
        "serving_version": "vllm==0.28.0",
        "context_tokens": 40960,
        "provenance": "open_weights",
        "sampling": {
            "temperature": 0.8,
            "top_p": 0.95,
            "max_tokens": 4096,
            "n": 8,
            "stop": [],
            "seed": 1234,
        },
    }


def test_an_unstated_revision_stays_none_rather_than_being_guessed() -> None:
    """A published manifest saying `null` is honest; one filled in with a plausible value is not."""
    entries = ModelRouter.from_config(_models()).manifest_entries()
    decomposer = next(entry for entry in entries if entry["role"] == "decomposer")
    assert decomposer["weights_revision"] is None
    assert decomposer["serving_version"] is None
    assert decomposer["context_tokens"] is None


def test_a_router_built_from_backends_directly_refuses_to_describe_itself() -> None:
    """Rather than emitting an empty or invented `models` array into a run manifest."""
    with pytest.raises(ValueError, match="cannot describe itself"):
        _router(prover=FakeBackend("m", ProvenanceClass.OPEN_WEIGHTS)).manifest_entries()


# --------------------------------------------------------------------------------------------
# Construction and teardown.
# --------------------------------------------------------------------------------------------


def test_the_backend_factory_is_injectable() -> None:
    """So an eventual `adapters/closed.py` -- or a test -- can supply something that is not an
    OpenAI-compatible HTTP client, without the router knowing what a backend is made of."""
    built: list[str] = []

    def factory(config: BackendConfig) -> ModelBackend:
        built.append(config.model_id)
        return FakeBackend(config.model_id, config.provenance)

    router = ModelRouter.from_config(_models(), factory=factory)
    assert sorted(built) == ["Goedel-LM/Goedel-Prover-V2-8B", "some/decomposer"]
    assert isinstance(router.backend_for(ModelRole.PROVER), FakeBackend)


def test_the_default_factory_builds_a_real_client_with_its_pinned_tokenizer() -> None:
    config = _models()[ModelRole.PROVER]
    backend = build_backend(config)
    assert isinstance(backend, CompletionsClient)
    assert backend.provenance is ProvenanceClass.OPEN_WEIGHTS


def test_closing_the_router_closes_the_transports_it_owns() -> None:
    """And tolerates backends that hold none, which is why `aclose` is guarded rather than
    assumed present on `ModelBackend`."""
    router = ModelRouter.from_config(_models())
    clients = [b for b in router.backends.values() if isinstance(b, CompletionsClient)]
    assert len(clients) == 2

    asyncio.run(router.aclose())

    mixed = ModelRouter(
        backends={
            ModelRole.PROVER: FakeBackend("m", ProvenanceClass.OPEN_WEIGHTS),
            ModelRole.DECOMPOSER: clients[0],
        }
    )
    asyncio.run(mixed.aclose())


def test_a_router_is_frozen_so_a_run_cannot_reroute_itself(**_: Any) -> None:
    """A run whose role mapping changed midway would produce a manifest describing something other
    than what happened, which is the one thing §7.3 exists to prevent."""
    router = _router(prover=FakeBackend("m", ProvenanceClass.OPEN_WEIGHTS))
    with pytest.raises(AttributeError):
        router.backends = {}  # type: ignore[misc]
