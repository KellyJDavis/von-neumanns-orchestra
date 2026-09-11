"""Pydantic bodies for the public API (spec §6.1).

"All request and response bodies are Pydantic models; the OpenAPI document is generated, not
written." These are that layer, and they are separate from `lean_agent_core.schemas` (the Pydantic
*mirror of every table*, spec §5.4) on purpose: a table's shape and an endpoint's shape are
different contracts, and collapsing them means every column added for internal bookkeeping becomes
public API by accident.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field, model_validator


class CreateRunRequest(BaseModel):
    """Spec §6.1's own model. `base_env` accepts a hex digest; the curated-alias form
    ("mathlib-stable") needs a registry of curated environments that does not exist yet, and
    inventing aliases nothing resolves would be a lie in the OpenAPI document."""

    source: str | None = None
    statement: str | None = None
    base_env: str
    policy: str = "SymbolicPortfolio"
    allow_sorry: bool = False
    axiom_allowlist: list[str] | None = None
    max_depth: int = 6
    budget_attempts: int = 8
    budget_tokens: int | None = None
    budget_kernel_ms: int | None = None
    reference_docs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_input(self) -> CreateRunRequest:
        if (self.source is None) == (self.statement is None):
            raise ValueError("exactly one of `source` or `statement` must be given")
        return self


class SealFailureBody(BaseModel):
    name: str
    statement: str
    reason: str
    diagnostics: list[str]


class AdmissionBody(BaseModel):
    """Spec §4.5's signals. Recorded and reported, never blocking."""

    closed_by: str | None = None
    closed_in_ms: int | None = None
    needs_auto_implicit: bool = False


class CreateRunResponse(BaseModel):
    run_id: uuid.UUID
    manifest_hash: str
    root_obligations: list[uuid.UUID]
    admission: dict[uuid.UUID, AdmissionBody]
    seal_failures: list[SealFailureBody]


class RunSpend(BaseModel):
    tokens: int
    kernel_ms: int
    attempts: int


class RunStatusResponse(BaseModel):
    """Spec §6.1: "Status, spend, counts by obligation status"."""

    run_id: uuid.UUID
    status: str
    created_at: str
    manifest_hash: str
    obligations_by_status: dict[str, int]
    spend: RunSpend


class ObligationSummary(BaseModel):
    id: uuid.UUID
    status: str
    depth: int
    priority: float
    is_root: bool
    decl_name: str
    goal_src: str


class ObligationPage(BaseModel):
    """Keyset pagination on `(created_at, id)` rather than `OFFSET`: a run's obligations are being
    inserted and updated while a client pages through them, and `OFFSET` over a moving set skips
    and repeats rows silently."""

    obligations: list[ObligationSummary]
    next_cursor: str | None = None


class ObligationDetail(ObligationSummary):
    """Spec §6.1: "Including `admission` signals and budget spend"."""

    run_id: uuid.UUID
    admission: AdmissionBody
    budget_attempts: int
    spent_attempts: int
    spent_tokens: int
    spent_kernel_ms: int
    bundle_sha: str | None
    sealed_olean_sha: str | None


class DagEdge(BaseModel):
    parent_id: uuid.UUID
    child_id: uuid.UUID
    group_id: uuid.UUID
    role: str


class DagResponse(BaseModel):
    """Spec §6.1: "Sub-DAG with groups and edge roles"."""

    root: uuid.UUID
    nodes: list[ObligationSummary]
    edges: list[DagEdge]


class VerdictBody(BaseModel):
    kind: str
    link_ok: bool
    replay_ok: bool
    axiom_audit_ok: bool
    axioms: list[str]
    elapsed_ms: int
    cache_hit: bool


class AttemptSummary(BaseModel):
    id: uuid.UUID
    obligation_id: uuid.UUID
    status: str
    policy_id: str
    started_at: str
    finished_at: str | None
    tokens_in: int
    tokens_out: int
    kernel_ms: int
    wallclock_ms: int
    verdict: VerdictBody | None = None


class PromptBody(BaseModel):
    """One prompt exactly as the model received it (spec §7.4: "exact rendered prompts (not
    reconstructions)").

    `text` is the stored token ids decoded with the tokenizer the trajectory recorded -- never the
    policy's messages re-rendered, which would be a reconstruction that silently disagrees with the
    record the day a template or tokenizer changes. When this deployment does not have that
    tokenizer, `text` is `None` and `token_ids` carries the ids themselves, with `note` saying why.
    """

    token_count: int
    text: str | None
    decoded_with: str | None
    token_ids: list[int] | None = None
    note: str | None = None


class CompletionBody(BaseModel):
    """One sample, decoded the same way as its prompt, with how it ended."""

    index: int
    token_count: int
    text: str | None
    #: `None` for an exchange recorded before M3.11, which stored none -- not a guessed `stop`.
    finish_reason: str | None
    logprob_sum: float
    token_ids: list[int] | None = None


class ExchangeBody(BaseModel):
    """One request an attempt made (`protocols.Exchange`) and every sample it got back."""

    index: int
    sampling: dict[str, Any]
    seed: int | None
    prompt: PromptBody
    completions: list[CompletionBody]


class StepBody(BaseModel):
    """`executor.TrajectoryStep`, as stored. Fields added after a row was written default."""

    label: str
    action: str
    ok: bool
    detail: str | None = None
    development: str | None = None
    diagnostics: list[str] = Field(default_factory=list)
    exchange: int | None = None


class ToolCallBody(BaseModel):
    step_index: int
    server: str
    tool: str
    trust: str
    ok: bool
    latency_ms: int
    args: str
    result: str | None


class VerdictDetail(VerdictBody):
    """The verdict in full: what leanserv wrote, including the accepted proof text."""

    kernels_agreeing: list[str]
    diagnostics: list[str]
    proof: str | None


class TrajectoryResponse(BaseModel):
    """Spec §6.1: "Rendered prompts, completions, tool calls, verdict".

    `provenance` is first and is not decoration: spec §7.1 makes it `NOT NULL` with no default
    because it decides whether a trajectory may be exported as training data at all.
    """

    attempt_id: uuid.UUID
    provenance: str
    model_id: str | None
    model_weights_hash: str | None = None
    tokenizer_revision: str | None = None
    #: The opening request's effective sampling and seed; each exchange carries its own.
    sampling: dict[str, Any] = Field(default_factory=dict)
    seed: int | None = None
    n_steps: int
    steps: list[StepBody]
    exchanges: list[ExchangeBody] = Field(default_factory=list)
    tool_calls: list[ToolCallBody] = Field(default_factory=list)
    verdict: VerdictDetail | None = None


class BaseEnvBody(BaseModel):
    digest: str
    recipe: dict[str, Any]
    toolchain_rev: str
    mathlib_rev: str
    curated: bool


class RegisterBaseEnvRequest(BaseModel):
    """Spec §6.1: "Register a project prelude; returns `digest`"."""

    imports: list[str]
    toolchain_rev: str
    mathlib_rev: str


class ArtifactResponse(BaseModel):
    """Spec §6.3 step 6's materialized file, plus its two checks.

    `complete` is the honest headline: a run with an unproved hole still has an artifact, and the
    field says whether it is finished rather than leaving a caller to infer it from the text.
    """

    run_id: uuid.UUID
    source: str
    complete: bool
    elaborates: bool
    links: bool
    holes: int
    unfilled: list[str]
    diagnostics: list[str]
