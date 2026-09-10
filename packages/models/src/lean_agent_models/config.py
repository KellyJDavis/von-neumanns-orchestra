"""Appendix B's `[models.<role>]` blocks, parsed into typed configuration.

```toml
[models.prover]
backend = "vllm"
endpoint = "http://vllm:8000"
model_id = "Goedel-LM/Goedel-Prover-V2-8B"
tokenizer_revision = "..."
provenance = "open_weights"
sampling = { temperature = 0.8, top_p = 0.95, max_tokens = 4096, n = 8 }
```

Spec's promise for this file is concrete -- "switching provers is one TOML line; a four-model
ablation is four config files and no code" -- so the parsing has to be strict enough that a typo
is an error rather than a silently different experiment. `tomllib` is in the standard library from
3.11, so this costs no dependency.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.protocols import SamplingParams
from lean_agent_core.roles import ModelRole


class ConfigError(ValueError):
    """The configuration does not describe a runnable model.

    Raised rather than defaulted, everywhere. A missing `model_id` defaulted to something would
    produce a run whose manifest names a model nobody chose, and spec §7.3 exists to make a
    published result reproducible from that manifest.
    """


#: Every key `[models.<role>]` understands. Anything else is an error rather than ignored: a
#: mistyped `temprature` that is silently dropped leaves the model sampling at a temperature the
#: config does not show, and the run manifest would record the config rather than the behaviour.
_KNOWN_KEYS = frozenset(
    {
        "backend",
        "endpoint",
        "model_id",
        "tokenizer_revision",
        "provenance",
        "seed",
        "sampling",
    }
)

_KNOWN_SAMPLING_KEYS = frozenset({"temperature", "top_p", "max_tokens", "n", "stop"})


@dataclass(frozen=True)
class BackendConfig:
    """One role's backend.

    `provenance` is **required and never inferred from `backend`.** Inferring "served by vLLM,
    therefore open weights" is wrong in the direction that matters: a closed model's weights can be
    served by vLLM too, and §7.1 makes the export rule turn on this value. It mirrors
    `trajectory.provenance` being `NOT NULL` with no default -- the same decision, one layer
    earlier, at the registration point §7.1 says provenance is derived from.
    """

    role: ModelRole
    backend: str
    model_id: str
    provenance: ProvenanceClass
    endpoint: str | None = None
    tokenizer_revision: str | None = None
    seed: int | None = None
    sampling: SamplingParams = field(default_factory=SamplingParams)


def _sampling(role: str, raw: Any) -> SamplingParams:
    if not isinstance(raw, dict):
        raise ConfigError(f"[models.{role}] sampling must be a table, got {type(raw).__name__}")
    unknown = set(raw) - _KNOWN_SAMPLING_KEYS
    if unknown:
        raise ConfigError(f"[models.{role}] unknown sampling key(s): {sorted(unknown)}")
    stop = raw.get("stop", ())
    if isinstance(stop, str):
        raise ConfigError(f"[models.{role}] sampling.stop must be a list of strings, not a string")
    defaults = SamplingParams()
    return SamplingParams(
        temperature=float(raw.get("temperature", defaults.temperature)),
        top_p=float(raw.get("top_p", defaults.top_p)),
        max_tokens=int(raw.get("max_tokens", defaults.max_tokens)),
        n=int(raw.get("n", defaults.n)),
        stop=tuple(stop),
    )


def parse_backend(role: str, raw: dict[str, Any]) -> BackendConfig:
    """One `[models.<role>]` table. Unknown keys and unknown roles are errors."""
    try:
        model_role = ModelRole(role)
    except ValueError:
        known = sorted(r.value for r in ModelRole)
        raise ConfigError(f"unknown model role {role!r}; spec §6.5 defines {known}") from None

    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        raise ConfigError(f"[models.{role}] unknown key(s): {sorted(unknown)}")

    for required in ("backend", "model_id", "provenance"):
        if required not in raw:
            raise ConfigError(f"[models.{role}] is missing required key {required!r}")

    try:
        provenance = ProvenanceClass(raw["provenance"])
    except ValueError:
        known = sorted(p.value for p in ProvenanceClass)
        raise ConfigError(
            f"[models.{role}] provenance {raw['provenance']!r} is not one of {known}"
        ) from None

    return BackendConfig(
        role=model_role,
        backend=str(raw["backend"]),
        model_id=str(raw["model_id"]),
        provenance=provenance,
        endpoint=str(raw["endpoint"]) if "endpoint" in raw else None,
        tokenizer_revision=(
            str(raw["tokenizer_revision"]) if "tokenizer_revision" in raw else None
        ),
        seed=int(raw["seed"]) if "seed" in raw else None,
        sampling=_sampling(role, raw.get("sampling", {})),
    )


def parse_models(document: dict[str, Any]) -> dict[ModelRole, BackendConfig]:
    """The `[models]` section of a whole configuration document."""
    models = document.get("models", {})
    if not isinstance(models, dict):
        raise ConfigError(f"[models] must be a table, got {type(models).__name__}")
    parsed = [parse_backend(role, raw) for role, raw in models.items()]
    return {config.role: config for config in parsed}


def load_models(path: Path) -> dict[ModelRole, BackendConfig]:
    return parse_models(tomllib.loads(path.read_text()))
