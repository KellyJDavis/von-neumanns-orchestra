"""Protocols shared across `lean_agent_core` and its consumers (spec Appendix A).

Each arrives when something implements *and* something consumes it, not before: a protocol with
no implementation is a guess about a shape. `BlobStore` came with M1.7; `LeanService` and `Policy`
come with M2.5, which is the first milestone with both an executor that calls one and a policy
that is one. `ModelBackend`, `ToolClient` and `Sink` are still absent for the same reason.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from lean_agent_core.actions import Action, Budget, ObligationContext
from lean_agent_core.enums import VerdictKind


class BlobStore(Protocol):
    async def put(self, data: bytes, media_type: str) -> bytes: ...
    async def get(self, digest: bytes) -> bytes: ...
    async def exists(self, digest: bytes) -> bool: ...
    def url(self, digest: bytes) -> str: ...


@dataclass(frozen=True)
class LinkOutcome:
    """What `/v1/link` reported (spec §6.2's `LinkResponse`), in core's own vocabulary.

    Mirrored rather than imported: `lean_agent_serv` depends on `lean_agent_core`, so core cannot
    import back without a cycle -- and it should not want to. A policy executor talks to *a* Lean
    service, and the HTTP client that leanserv happens to serve is one implementation.
    """

    kind: VerdictKind
    link_ok: bool
    replay_ok: bool
    axiom_audit_ok: bool
    axioms: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    elapsed_ms: int = 0

    @property
    def proved(self) -> bool:
        """All three checks passed. Note this is *not* the §1.1 acceptance predicate -- that also
        requires seal integrity against the obligation's own digest, which only `mark_proved` can
        judge, since only the database holds what the digest is supposed to be."""
        return self.link_ok and self.replay_ok and self.axiom_audit_ok


@dataclass(frozen=True)
class CheckOutcome:
    """What `/v1/check` reported: did this body elaborate. No verdict is written, and none should
    be -- this is the exploration endpoint."""

    ok: bool
    diagnostics: tuple[str, ...] = ()
    cache_hit: bool = False
    elapsed_ms: int = 0


@dataclass(frozen=True)
class SealGoalRequest:
    """One goal to seal. `level_params` is required whenever `statement` names a universe: sealing
    forces `autoImplicit false`, so a free universe name is an error rather than something Lean
    binds (M2.1.3's finding)."""

    name: str
    statement: str
    level_params: tuple[str, ...] = ()


@dataclass(frozen=True)
class SealedGoal:
    decl_name: str
    goal_src: str
    goal_digest: str
    level_params: tuple[str, ...]
    diagnostics: tuple[str, ...]
    ok: bool


@dataclass(frozen=True)
class SealOutcome:
    """`goals` is parallel to the request's, so a caller creates obligations for the entries that
    sealed and reports the rest -- spec §6.1's "a submission with ten goals of which one does not
    elaborate creates nine obligations and reports the tenth"."""

    ok: bool
    goals: tuple[SealedGoal, ...]
    bundle_source: str
    bundle_digest: str


@dataclass(frozen=True)
class DecomposedLemma:
    name: str
    statement: str
    level_params: tuple[str, ...]
    round_trips: bool
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class DecomposeOutcome:
    """`ok` with no `lemmas` means the development had no `sorry`; `ok=False` means it did not
    elaborate. A lemma with `round_trips=False` must not become an obligation: its printed
    statement does not seal back to the `Expr` it came from (M2.1.3)."""

    ok: bool
    lemmas: tuple[DecomposedLemma, ...]
    reassembly: str
    diagnostics: tuple[str, ...]


class LeanService(Protocol):
    """The Lean Execution Service as a policy executor sees it (spec §6.2, Appendix A).

    `check` and `link` are both here, and the split between them is not a convenience -- it is
    forced by the schema. `verdict.attempt_id` is a primary key, so an attempt has **at most one
    verdict, ever**, while a portfolio makes many submissions inside one attempt. So a policy
    *screens* with `check` (cheap, cached, writes nothing) and spends its single `link` on the
    candidate that passed. Spec's own endpoint table says as much in one line each: `/v1/check`
    "elaborate a body against a base env"; `/v1/link` "§4.2, then replay and audit; **writes the
    verdict row**".

    `check` takes `bundle_sha` because a screening development names the sealed goal constant, and
    a worker without the bundle on its path cannot resolve it.

    `seal` and `decompose` join them for ingestion (M2.6), which is a caller of this service that
    is not an executor: it turns a submission into obligations before any policy runs.
    """

    async def check(
        self,
        *,
        base_env_digest: str,
        body: str,
        bundle_sha: str | None = None,
        timeout_ms: int | None = None,
    ) -> CheckOutcome: ...

    async def seal(
        self,
        *,
        base_env_digest: str,
        goals: Sequence[SealGoalRequest],
        timeout_ms: int | None = None,
    ) -> SealOutcome: ...

    async def decompose(
        self, *, base_env_digest: str, development: str, timeout_ms: int | None = None
    ) -> DecomposeOutcome: ...

    async def link(
        self,
        *,
        attempt_id: uuid.UUID,
        obligation_id: uuid.UUID,
        base_env_digest: str,
        bundle_sha: str,
        goal: str,
        entry: str,
        development: str,
        timeout_ms: int | None = None,
    ) -> LinkOutcome: ...


class Policy(Protocol):
    """Spec §6.6/Appendix A. A policy proposes `Action`s and performs none of them.

    `roles` is the set of `ModelRole`s it needs -- empty for `SymbolicPortfolio`, which is what
    makes Phase 2's "zero model calls anywhere in the codebase" checkable rather than asserted.
    `tools` is the allowlist the executor enforces against `CallTool`.

    `config_hash` goes into the run manifest (spec §7.3), so a published result names not just
    which policy ran but which configuration of it. A policy whose behaviour can vary and whose
    hash cannot is unreproducible in exactly the way the manifest exists to prevent.

    `propose` is an async *generator*: actions arrive one at a time and the executor may stop
    consuming at any point -- when a proof lands, when the budget runs out. A policy that returned
    a list would compute every proposal even after the first one succeeded, which for a timed
    portfolio is the whole cost.

    Every attribute is declared read-only (a `@property`, not a bare annotation), which is not a
    style choice. A plain `id: str` in a Protocol demands a *settable* attribute, so a frozen
    dataclass -- the natural way to write a policy whose configuration cannot drift from its
    `config_hash` -- does not satisfy it. mypy caught exactly that against `SymbolicPortfolio`.
    Read-only is also the honest requirement: a policy whose `config_hash` could be reassigned
    after the run manifest recorded it is unreproducible in precisely the way §7.3 exists to
    prevent.
    """

    @property
    def id(self) -> str: ...

    @property
    def config_hash(self) -> bytes: ...

    @property
    def tools(self) -> frozenset[str]: ...

    @property
    def roles(self) -> frozenset[str]: ...

    def propose(self, ctx: ObligationContext, budget: Budget) -> AsyncIterator[Action]: ...


@dataclass(frozen=True)
class ToolResult:
    """Placeholder shape for `CallTool`, defined alongside `ToolClient`'s absence: the executor
    rejects tool calls today, and this exists so that rejection can be typed."""

    tool: str
    ok: bool
    payload: dict[str, object] = field(default_factory=dict)
