"""Postgres ENUM types mirrored as Python enums (spec §5.2), shared by the ORM and Pydantic layers
so a single definition backs the database type, the SQLAlchemy column, and the API schema."""

from enum import StrEnum


class ObligationStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DECOMPOSED = "decomposed"
    PROVED = "proved"
    FAILED = "failed"
    BLOCKED = "blocked"
    ABANDONED = "abandoned"


class AttemptStatus(StrEnum):
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"
    INFRA_ERROR = "infra_error"


class VerdictKind(StrEnum):
    PROVED = "proved"
    REFUTED = "refuted"
    ERRORS = "errors"
    TIMEOUT = "timeout"
    OOM = "oom"
    INFRA_ERROR = "infra_error"


class TrustClass(StrEnum):
    KERNEL_CHECKED = "kernel_checked"
    ADVISORY = "advisory"
    RETRIEVAL = "retrieval"


class ProvenanceClass(StrEnum):
    OPEN_WEIGHTS = "open_weights"
    SYMBOLIC = "symbolic"
    HUMAN = "human"
    CLOSED_API_EVAL_ONLY = "closed_api_eval_only"
