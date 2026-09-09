"""Frozen Pydantic v2 mirror of every table in `orm.py` (spec §5.4): "No dict crosses a process
boundary untyped." These are the single source of truth for the OpenAPI document, MCP tool
schemas, and on-disk JSONL -- not a convenience layer on top of the ORM models, which stay
separate so a Pydantic model can be constructed (e.g. from a JSONL line, or an API request) with
no SQLAlchemy session in scope at all.

Field-for-field faithful to `orm.py`/spec §5.3: a `NOT NULL` column is a required field here, a
nullable column is `X | None = None`. All models are frozen -- once constructed, a schema value
is never mutated in place.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from lean_agent_core.enums import (
    AttemptStatus,
    ObligationStatus,
    ProvenanceClass,
    TrustClass,
    VerdictKind,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


class BaseEnvSchema(_Frozen):
    digest: bytes
    recipe: dict[str, Any]
    toolchain_rev: str
    mathlib_rev: str
    snapshot_path: str | None = None
    snapshot_bytes: int | None = None
    curated: bool = False
    created_at: datetime
    last_used_at: datetime | None = None


class RunSchema(_Frozen):
    id: UUID
    tenant_id: UUID
    base_env_digest: bytes
    status: str
    manifest: dict[str, Any]
    manifest_hash: bytes
    allow_sorry: bool = False
    axiom_allowlist: list[str] = ["propext", "Classical.choice", "Quot.sound"]
    max_depth: int = 6
    budget_tokens: int | None = None
    budget_wallclock_ms: int | None = None
    budget_kernel_ms: int | None = None
    created_at: datetime


class ObligationSchema(_Frozen):
    id: UUID
    run_id: UUID
    status: ObligationStatus = ObligationStatus.OPEN
    depth: int = 0
    priority: float = 0
    is_root: bool = False

    # identity (spec §4)
    base_env_digest: bytes
    goal_digest: bytes
    sealed_olean_sha: bytes
    goal_src: str
    decl_name: str
    admission: dict[str, Any] = {}

    # budgets
    budget_attempts: int = 8
    budget_tokens: int = 1_000_000
    budget_kernel_ms: int = 600_000
    spent_attempts: int = 0
    spent_tokens: int = 0
    spent_kernel_ms: int = 0

    proof_blob: bytes | None = None
    created_by_policy: str | None = None
    created_at: datetime
    updated_at: datetime


class ObligationEdgeSchema(_Frozen):
    parent_id: UUID
    child_id: UUID
    group_id: UUID
    role: str
    reassembly_blob: bytes | None = None


class AttemptSchema(_Frozen):
    id: UUID
    obligation_id: UUID
    run_id: UUID
    status: AttemptStatus = AttemptStatus.CLAIMED
    policy_id: str
    policy_config_hash: bytes
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    started_at: datetime
    finished_at: datetime | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    kernel_ms: int = 0
    wallclock_ms: int = 0


class VerdictSchema(_Frozen):
    attempt_id: UUID
    obligation_id: UUID
    kind: VerdictKind
    link_ok: bool
    replay_ok: bool
    axiom_audit_ok: bool
    sealed_olean_sha_observed: bytes | None = None
    axioms: list[str] | None = None
    kernels_agreeing: list[str] | None = None
    messages_blob: bytes | None = None
    infotree_blob: bytes | None = None
    proof_blob: bytes | None = None
    elapsed_ms: int
    cache_hit: bool = False
    toolchain_rev: str
    mathlib_rev: str
    created_at: datetime


class TrajectorySchema(_Frozen):
    attempt_id: UUID
    # No default (spec §7.1, §5.3): provenance must be asserted explicitly, never silently
    # defaulted, so this field intentionally has no `= ...` here either.
    provenance: ProvenanceClass
    model_id: str | None = None
    model_weights_hash: str | None = None
    tokenizer_revision: str | None = None
    sampling: dict[str, Any]
    seed: int | None = None
    steps_blob: bytes
    token_ids_blob: bytes | None = None
    logprobs_blob: bytes | None = None
    n_steps: int
    created_at: datetime


class ToolCallSchema(_Frozen):
    id: int
    attempt_id: UUID
    step_index: int
    server: str
    tool: str
    trust: TrustClass
    args_blob: bytes
    result_blob: bytes | None = None
    ok: bool
    latency_ms: int
    cost_usd: Decimal = Decimal(0)


class VerificationCacheSchema(_Frozen):
    cache_key: bytes
    kind: VerdictKind
    axioms: list[str] | None = None
    messages_blob: bytes | None = None
    infotree_blob: bytes | None = None
    elapsed_ms: int
    toolchain_rev: str
    mathlib_rev: str
    hits: int = 0
    created_at: datetime
    last_hit_at: datetime | None = None


class BlobSchema(_Frozen):
    sha256: bytes
    size_bytes: int
    media_type: str
    location: str
    tenant_id: UUID | None = None
    created_at: datetime
