"""SQLAlchemy 2.0 declarative models for the schema in spec §5.3.

Column-for-column faithful to the spec's DDL, including its defaults and indexes. Anything not
explicitly justified there (naming, nullability, index shape) should match spec exactly rather
than "improve" on it -- schema is architecture (spec §8's MVP table), and drift here is exactly
the kind of thing `deploy/grants.sql` (§5.5, M1.6) will need to match precisely.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    Text,
)
from sqlalchemy import (
    Enum as PgEnum,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

from lean_agent_core.enums import (
    AttemptStatus,
    ObligationStatus,
    ProvenanceClass,
    TrustClass,
    VerdictKind,
)


class Base(DeclarativeBase):
    # Every spec `timestamptz` column maps here instead of repeating `DateTime(timezone=True)`
    # on each one; a bare `datetime` column would otherwise map to Postgres `TIMESTAMP` without
    # a timezone, silently dropping spec's requirement.
    type_annotation_map: ClassVar[dict[type, object]] = {datetime: DateTime(timezone=True)}


def _pg_enum(enum_cls: type, name: str) -> PgEnum:
    """A Postgres ENUM type using each member's lowercase `.value` (spec §5.2), not its uppercase
    Python name."""
    return PgEnum(enum_cls, name=name, values_callable=lambda e: [member.value for member in e])


class BaseEnv(Base):
    __tablename__ = "base_env"

    digest: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    recipe: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    toolchain_rev: Mapped[str] = mapped_column(Text, nullable=False)
    mathlib_rev: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_path: Mapped[str | None] = mapped_column(Text)
    snapshot_bytes: Mapped[int | None] = mapped_column(BigInteger)
    curated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column()


class Run(Base):
    __tablename__ = "run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    base_env_digest: Mapped[bytes] = mapped_column(
        LargeBinary, ForeignKey("base_env.digest"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    manifest_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    allow_sorry: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    axiom_allowlist: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default="{propext,Classical.choice,Quot.sound}",
    )
    max_depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default="6")
    budget_tokens: Mapped[int | None] = mapped_column(BigInteger)
    budget_wallclock_ms: Mapped[int | None] = mapped_column(BigInteger)
    budget_kernel_ms: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (Index(None, "tenant_id", created_at.desc()),)


class Obligation(Base):
    __tablename__ = "obligation"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[ObligationStatus] = mapped_column(
        _pg_enum(ObligationStatus, "obligation_status"),
        nullable=False,
        server_default=ObligationStatus.OPEN.value,
    )
    depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    priority: Mapped[float] = mapped_column(Double, nullable=False, server_default="0")
    is_root: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # identity (spec §4)
    base_env_digest: Mapped[bytes] = mapped_column(
        LargeBinary, ForeignKey("base_env.digest"), nullable=False
    )
    goal_digest: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sealed_olean_sha: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    goal_src: Mapped[str] = mapped_column(Text, nullable=False)
    decl_name: Mapped[str] = mapped_column(Text, nullable=False)
    admission: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")

    # budgets
    budget_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="8")
    budget_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1000000")
    budget_kernel_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="600000"
    )
    spent_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    spent_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    spent_kernel_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    proof_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_by_policy: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (
        Index(
            "obligation_schedulable",
            "run_id",
            "status",
            priority.desc(),
            "depth",
            postgresql_where=(status == ObligationStatus.OPEN.value),
        ),
        Index(None, "goal_digest"),
    )


class ObligationEdge(Base):
    __tablename__ = "obligation_edge"

    parent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("obligation.id", ondelete="CASCADE"), nullable=False
    )
    child_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("obligation.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    reassembly_blob: Mapped[bytes | None] = mapped_column(LargeBinary)

    __table_args__ = (
        PrimaryKeyConstraint("parent_id", "child_id", "group_id"),
        Index(None, "child_id"),
        Index(None, "group_id"),
    )


class Attempt(Base):
    __tablename__ = "attempt"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    obligation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("obligation.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[AttemptStatus] = mapped_column(
        _pg_enum(AttemptStatus, "attempt_status"),
        nullable=False,
        server_default=AttemptStatus.CLAIMED.value,
    )
    policy_id: Mapped[str] = mapped_column(Text, nullable=False)
    policy_config_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    heartbeat_at: Mapped[datetime | None] = mapped_column()
    started_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column()
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    kernel_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    wallclock_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    __table_args__ = (
        Index(
            "attempt_expired",
            "lease_expires_at",
            postgresql_where=status.in_([AttemptStatus.CLAIMED.value, AttemptStatus.RUNNING.value]),
        ),
        Index(None, "obligation_id", started_at.desc()),
    )


class Verdict(Base):
    __tablename__ = "verdict"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attempt.id", ondelete="CASCADE"), primary_key=True
    )
    obligation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("obligation.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[VerdictKind] = mapped_column(_pg_enum(VerdictKind, "verdict_kind"), nullable=False)
    link_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    replay_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    axiom_audit_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sealed_olean_sha_observed: Mapped[bytes | None] = mapped_column(LargeBinary)
    axioms: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    kernels_agreeing: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    messages_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    infotree_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    proof_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    elapsed_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    toolchain_rev: Mapped[str] = mapped_column(Text, nullable=False)
    mathlib_rev: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (Index(None, "obligation_id", "kind"),)


class Trajectory(Base):
    __tablename__ = "trajectory"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attempt.id", ondelete="CASCADE"), primary_key=True
    )
    # No default (spec §7.1, §5.3): provenance must be asserted explicitly by the caller that
    # registered the model backend, never silently defaulted.
    provenance: Mapped[ProvenanceClass] = mapped_column(
        _pg_enum(ProvenanceClass, "provenance_class"), nullable=False
    )
    model_id: Mapped[str | None] = mapped_column(Text)
    model_weights_hash: Mapped[str | None] = mapped_column(Text)
    tokenizer_revision: Mapped[str | None] = mapped_column(Text)
    sampling: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    seed: Mapped[int | None] = mapped_column(BigInteger)
    steps_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_ids_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    logprobs_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    n_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (Index(None, "provenance", "model_id"),)


class ToolCall(Base):
    __tablename__ = "tool_call"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attempt.id", ondelete="CASCADE"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    server: Mapped[str] = mapped_column(Text, nullable=False)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    trust: Mapped[TrustClass] = mapped_column(_pg_enum(TrustClass, "trust_class"), nullable=False)
    args_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    result_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")

    __table_args__ = (Index(None, "attempt_id", "step_index"),)


class VerificationCache(Base):
    __tablename__ = "verification_cache"

    cache_key: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    kind: Mapped[VerdictKind] = mapped_column(_pg_enum(VerdictKind, "verdict_kind"), nullable=False)
    axioms: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    messages_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    infotree_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    elapsed_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    toolchain_rev: Mapped[str] = mapped_column(Text, nullable=False)
    mathlib_rev: Mapped[str] = mapped_column(Text, nullable=False)
    hits: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    last_hit_at: Mapped[datetime | None] = mapped_column()


class Blob(Base):
    __tablename__ = "blob"

    sha256: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
