"""Protocols shared across `lean_agent_core` and its consumers (spec Appendix A).

Each arrives when something implements *and* something consumes it, not before: a protocol with
no implementation is a guess about a shape. `BlobStore` came with M1.7; `LeanService` and `Policy`
come with M2.5, which is the first milestone with both an executor that calls one and a policy
that is one. `ModelBackend` is the one deliberate exception, added in M3.1 ahead of the client that implements
it (M3.4). The rule exists to stop a *guessed* shape being frozen, and this one is not guessed: the
request and response types below were checked field by field against a real vLLM 0.28.0 serving
`/v1/completions` -- token-id prompts accepted and echoed back, per-token logprobs returned -- and
the types are this milestone's actual subject. `ToolClient` and `Sink` are still absent, with no
such justification.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from lean_agent_core.actions import Action, Budget, ObligationContext
from lean_agent_core.enums import ProvenanceClass, VerdictKind
from lean_agent_core.roles import ModelRole


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
    #: Transitive axiom cone of everything the body newly declared; empty when it did not
    #: elaborate. Separate from `ok` because it has to be: a `sorry` is a *warning* in Lean, so a
    #: body whose proof is `sorry` -- or whose tactic (`apply?`, `exact?`, `rw?`) only partially
    #: closed the goal and left one behind -- elaborates with `ok=True`. Anything screening proof
    #: candidates must consult this; `ok` alone will accept a `sorry`.
    axioms: tuple[str, ...] = ()


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

    It is a generator in the full sense, not only an iterator: after performing an action the
    executor **sends back what it observed** (`Observation`), so `response = yield
    RequestCompletion(...)` is how a policy sees the tokens it asked for. Spec writes the return
    type as `AsyncIterator[Action]`, which has no channel back at all -- and every Phase 3 policy
    needs one: `WholeProofSampler` cannot submit samples it was never shown, and `RepairLoop`'s
    "feed diagnostics back" is the definition of the thing. The alternatives were worse: a mutable
    "observations" object the policy polls is hidden state a replay would have to reconstruct,
    whereas a sent value is exactly one recorded input per action. `SymbolicPortfolio` ignores what
    is sent, which costs it nothing -- `asend(None)` on a generator is `__anext__`.

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
    def roles(self) -> frozenset[ModelRole]: ...

    def propose(
        self, ctx: ObligationContext, budget: Budget
    ) -> AsyncGenerator[Action, Observation | None]: ...


@dataclass(frozen=True)
class ToolResult:
    """Placeholder shape for `CallTool`, defined alongside `ToolClient`'s absence: the executor
    rejects tool calls today, and this exists so that rejection can be typed."""

    tool: str
    ok: bool
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SamplingParams:
    """What was asked of the model, as opposed to what it answered.

    Separate from `CompletionRequest` because it is addressed three times over: it is part of the
    response cache key (spec §6.5: `sha256(prompt_tokens) ‖ model_id ‖ canonical(sampling) ‖
    seed`), it is stored on `trajectory.sampling`, and it comes from one `[models.<role>]` config
    block. `seed` is deliberately *not* a member -- spec's cache key names it separately, and
    folding it in would make two runs that differ only by seed look like one cache entry.
    """

    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 1024
    n: int = 1
    stop: tuple[str, ...] = ()

    def canonical(self) -> dict[str, object]:
        """The form that goes into a cache key and into `trajectory.sampling`.

        A plain dict with sorted keys at the point of use rather than a JSON string here, so the
        same value can be hashed by the cache and stored as `jsonb` without one of them
        re-serializing the other's output and drifting.
        """
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "n": self.n,
            "stop": list(self.stop),
        }


@dataclass(frozen=True)
class CompletionRequest:
    """One request to a model backend, as **token ids** (spec §6.5).

    `prompt_token_ids`, never a prompt string, and that is the load-bearing decision in this whole
    layer. Spec: rendering the chat template client-side and sending the id list is required
    because `tokenize=False` output "does not re-tokenize to the identity around special tokens and
    whitespace", and server-side templating "loses the exact token sequence entirely, which breaks
    replay and on-policy RL". A `str` field here would make that mistake available.
    """

    prompt_token_ids: tuple[int, ...]
    sampling: SamplingParams = field(default_factory=SamplingParams)
    seed: int | None = None
    #: Wallclock budget for the whole request. `None` defers to the backend's own default.
    timeout_ms: int | None = None


@dataclass(frozen=True)
class Completion:
    """One sample. A request with `n > 1` yields several.

    `logprobs` is required and parallel to `token_ids`, not optional: spec §6.5 says to request
    logprobs on every sampled token and store them, because "recomputing behavior-policy logprobs
    later produces the train/inference mismatch", and §9 lists them among the things that *cannot
    be recomputed correctly later*. Making the field non-optional means a backend that cannot
    supply them fails loudly at the boundary rather than writing a NULL nobody notices until
    training. That failure mode is real and observed: Ollama's OpenAI-compatible layer accepts
    `logprobs` and silently drops it.
    """

    token_ids: tuple[int, ...]
    logprobs: tuple[float, ...]
    text: str
    finish_reason: str


@dataclass(frozen=True)
class CompletionResponse:
    """What a backend answered, carrying everything a trajectory row needs (spec §5.3).

    `prompt_token_ids` is echoed back rather than assumed to equal the request's: replay compares
    what the server actually saw, and a server that re-tokenized would be caught here rather than
    silently producing a trajectory that cannot be replayed.
    """

    completions: tuple[Completion, ...]
    prompt_token_ids: tuple[int, ...]
    model_id: str
    #: Recorded onto `trajectory` so a published result names the exact weights and tokenizer
    #: (spec §7.3's manifest). `None` where a backend cannot report them, which is itself worth
    #: seeing in the row.
    model_weights_hash: str | None = None
    tokenizer_revision: str | None = None
    cache_hit: bool = False
    elapsed_ms: int = 0
    #: What was actually *asked* -- the deployment's configured sampling with the policy's
    #: overrides applied, and the seed that won. `None` from a backend, which only ever sees the
    #: merged request; filled by the completion service that did the merging.
    #:
    #: Added in M3.9, when the first real policy asked for no overrides at all and its trajectory
    #: recorded `sampling = {}` and `seed = NULL` while the model sampled 4 completions at
    #: temperature 0.8 under seed 1234. `trajectory.sampling` is what makes a run reproducible
    #: from its record (§7.3); recording the policy's *request* instead of the *effective* values
    #: was right only for policies that override everything, which is the opposite of what §6.5
    #: asks ("four config files and no code").
    sampling: SamplingParams | None = None
    seed: int | None = None


#: What the executor sends back into `Policy.propose` after performing an action.
#:
#: * `RequestCompletion` -> the `CompletionResponse`, every sample of it.
#: * `SubmitProof` screened out by `/v1/check` -> that `CheckOutcome`, diagnostics and axiom cone
#:   included, so a policy can tell "did not elaborate" from "elaborated via `sorry`".
#:
#: Nothing else is ever sent, and the absences are the design rather than gaps: a `SubmitProof`
#: that reaches `/v1/link` ends the attempt (its one verdict is spent), so there is no later point
#: at which a policy could act on the result; `Abandon` ends the stream by definition; and
#: `CallTool`/`Decompose` are refused before anything happens. `None` is sent on the first step,
#: which is how a generator is started.
Observation = CheckOutcome | CompletionResponse


@dataclass(frozen=True)
class Exchange:
    """One request an attempt made, and what came back: the unit a trajectory records.

    Added in M3.10, when `RepairLoop` became the first policy to make more than one request per
    attempt. Until then a trajectory held one prompt and every completion, so a second request's
    completions would have been stored against the *first* request's prompt -- a pairing that is
    silently wrong for replay and for RL, which needs each completion with the exact context it
    was conditioned on. `sampling` and `seed` are per exchange for the same reason: a repair asks
    for one sample where the opening request asked for several.
    """

    prompt_token_ids: tuple[int, ...]
    completions: tuple[Completion, ...]
    #: The effective sampling (`SamplingParams.canonical()`) when the service reported it, else
    #: the policy's own overrides -- the most specific account of what was asked that exists.
    sampling: dict[str, object]
    seed: int | None = None


class ModelBackend(Protocol):
    """Spec Appendix A. One serving endpoint behind one `ModelRole`.

    `provenance` is an attribute of the *backend*, not of a call, because §7.1 derives
    `trajectory.provenance` "from `ModelBackend.provenance` at model registration time. It is never
    asserted by whatever code is writing the trajectory row." A backend that could be asked for its
    provenance per call would let the caller choose, which is the thing that rule prevents.

    Read-only properties for the reason `Policy` documents: a bare annotation in a Protocol demands
    a settable attribute, which a frozen implementation cannot satisfy.
    """

    @property
    def id(self) -> str: ...

    @property
    def provenance(self) -> ProvenanceClass: ...

    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...

    async def tokenize(self, text: str) -> tuple[int, ...]: ...
