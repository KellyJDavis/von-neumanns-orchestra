"""`ModelRole` to backend (spec §6.5).

> Policies request a **role**, never a model. Switching provers is one TOML line; a four-model
> ablation is four config files and no code.

That indirection is the module's entire purpose, and it is worth being precise about what it buys:
not tidiness, but keeping the ablation matrix out of the source tree. A policy naming
`Goedel-Prover-V2-8B` directly would make "run this against three provers" a code change three
times over, and would make the run manifest's account of what served the run a guess.

Two things are enforced here rather than left to whoever wires a run together.

**A policy's declared roles must all be configured, checked before the run starts.** `Policy.roles`
exists precisely so this is answerable up front; discovering a missing `decomposer` at the moment a
policy first asks for one wastes everything already spent and reports it as a mid-run failure.

**Provenance travels with the backend.** §7.1 derives `trajectory.provenance` "from
`ModelBackend.provenance` at model registration time. It is never asserted by whatever code is
writing the trajectory row." Registration is here, so this is where a run can be asked what
provenances it is about to mix -- which is what makes the corpus exporter's rule checkable before
a corpus exists rather than after.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.protocols import ModelBackend
from lean_agent_core.roles import ModelRole

from lean_agent_models.client import CompletionsClient
from lean_agent_models.config import BackendConfig, ConfigError
from lean_agent_models.template import TemplateError, load_chat_tokenizer

#: Builds one backend from its configuration. Injectable so a test -- or an eventual
#: `adapters/closed.py` -- can supply something other than an OpenAI-compatible HTTP client without
#: the router knowing what a backend is made of.
BackendFactory = Callable[[BackendConfig], ModelBackend]


class RoleNotConfigured(ConfigError):
    """A role was asked for that no `[models.<role>]` block describes.

    A `ConfigError` rather than a runtime failure: the fix is always in configuration, and saying
    so in the type keeps it from being caught alongside genuine backend trouble.
    """


def build_backend(
    config: BackendConfig, *, client: httpx.AsyncClient | None = None
) -> ModelBackend:
    """The default factory: an OpenAI-compatible client with its pinned tokenizer.

    The tokenizer is loaded (and digest-checked, when `tokenizer_revision` is set) at *build* time,
    not at first use. A misconfigured tokenizer path should stop a run before it spends anything,
    and §7.1's "registration time" is the moment this happens.
    """
    if config.tokenizer_dir is None:
        raise ConfigError(
            f"[models.{config.role}] has no `tokenizer_dir`. §6.5 renders the chat template "
            "client-side, so this system cannot prompt a model whose tokenizer it does not have."
        )
    try:
        tokenizer = load_chat_tokenizer(
            config.tokenizer_dir, expect_sha256=config.tokenizer_revision
        )
    except TemplateError as exc:
        # Re-typed deliberately. `TemplateError` is a `ModelBackendError`, which is the right kind
        # of thing at *call* time -- a backend that could not produce a prompt. Reached from here
        # it means something else: the configuration names a tokenizer that is missing, or is not
        # the one it was pinned to. That is a `ConfigError`, and saying so lets a caller guarding
        # startup catch every "your configuration is wrong" in one place.
        raise ConfigError(f"[models.{config.role}] {exc}") from exc
    return CompletionsClient(config, tokenizer, client=client)


@dataclass(frozen=True)
class ModelRouter:
    """Role to backend, fixed for the life of a run.

    Frozen, and built once: a run whose role mapping could change midway would produce a manifest
    that describes something other than what happened, which is the one thing §7.3 exists to
    prevent.
    """

    backends: Mapping[ModelRole, ModelBackend]
    #: Kept beside the backends because the manifest needs what a `ModelBackend` does not expose --
    #: the weights revision, the serving version, the sampling defaults. A backend knows how to
    #: complete; the configuration knows what it is. `None` when a caller supplied backends
    #: directly (a test, mostly), in which case there is nothing to describe and
    #: `manifest_entries` says so rather than inventing entries.
    configs: Mapping[ModelRole, BackendConfig] | None = None

    @classmethod
    def from_config(
        cls,
        models: Mapping[ModelRole, BackendConfig],
        *,
        factory: BackendFactory = build_backend,
    ) -> ModelRouter:
        """Build every configured backend eagerly.

        Eagerly, so a bad tokenizer path or an unreadable pin fails before the run claims work
        rather than at whatever hour the first `decomposer` call happens.
        """
        return cls(
            backends={role: factory(config) for role, config in models.items()},
            configs=dict(models),
        )

    @property
    def roles(self) -> frozenset[ModelRole]:
        return frozenset(self.backends)

    def backend_for(self, role: ModelRole) -> ModelBackend:
        backend = self.backends.get(role)
        if backend is None:
            configured = sorted(r.value for r in self.backends)
            raise RoleNotConfigured(
                f"no backend for role {role.value!r}; configured roles are {configured}. "
                f"Add a [models.{role.value}] block."
            )
        return backend

    def require(self, roles: frozenset[ModelRole]) -> None:
        """Check a policy's declared needs before the run starts.

        `SymbolicPortfolio` declares none and so passes trivially, which is the same fact that
        makes Phase 2's "zero model calls" checkable: a policy that needs nothing cannot be
        misconfigured into needing something.
        """
        missing = sorted(role.value for role in roles - self.roles)
        if missing:
            raise RoleNotConfigured(
                f"policy needs role(s) {missing} that no [models.*] block configures; "
                f"configured roles are {sorted(r.value for r in self.roles)}"
            )

    def provenances(self) -> frozenset[ProvenanceClass]:
        """Every provenance this router can produce.

        Asking before a run is what lets §7.1's export rule be enforced at configuration time. The
        exporter "raises (does not filter)" on anything outside `{open_weights, symbolic, human}`,
        and finding that out after a corpus has been collected is finding out too late.
        """
        return frozenset(backend.provenance for backend in self.backends.values())

    def manifest_entries(self) -> list[dict[str, Any]]:
        """The manifest's `models` array (§7.3), in role order.

        `weights_revision` and `serving_version` come from configuration rather than from the
        backend because neither is derivable from a model id: the same id serves different weights
        across revisions, and the same weights give different token ids across serving versions.
        `None` where a deployment did not say -- which is itself worth seeing in a published
        manifest rather than being filled in with a guess.
        """
        if self.configs is None:
            raise ValueError("this router was built without configs and cannot describe itself")
        return [
            {
                "role": role.value,
                "backend": config.backend,
                "model_id": config.model_id,
                "weights_revision": config.weights_revision,
                "tokenizer_revision": config.tokenizer_revision,
                "serving_version": config.serving_version,
                # What each request's `max_tokens` was capped against, so part of what decided
                # how long an answer could be.
                "context_tokens": config.context_tokens,
                "provenance": config.provenance.value,
                "sampling": {**config.sampling.canonical(), "seed": config.seed},
            }
            for role, config in sorted(self.configs.items(), key=lambda item: item[0].value)
        ]

    async def aclose(self) -> None:
        """Close every backend that owns a transport, concurrently.

        `return_exceptions=True` so one backend failing to close does not strand the rest -- the
        leak M1.8.5 found the hard way, where a swallowed teardown error left worker processes
        running until garbage collection noticed.
        """
        closers = [
            backend.aclose()
            for backend in self.backends.values()
            # `hasattr` rather than a cast: `ModelBackend` (Appendix A) has no `aclose`, and a
            # backend that holds no transport -- a test double, an eventual in-process adapter --
            # has nothing to close. mypy narrows this correctly, so it stays type-checked.
            if hasattr(backend, "aclose")
        ]
        if closers:
            await asyncio.gather(*closers, return_exceptions=True)
