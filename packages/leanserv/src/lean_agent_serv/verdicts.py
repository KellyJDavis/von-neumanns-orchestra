"""Writes `verdict` rows -- spec §5.5/§6.4: `leanserv` is the *only* writer of verdicts; workers
request a check and observe the outcome through this path, never by transcribing a result into
`obligation.status` themselves. `deploy/grants.sql` (M1.6) is what actually enforces this at the
database (`GRANT INSERT ON verdict TO leanserv`, with no such grant to `app`) -- this module is
the one thing on the `leanserv` side of that boundary that ever calls it, by construction: nothing
else in this package touches the `verdict` table.

Depends on a caller-supplied `async_sessionmaker` authenticated as the `leanserv` role, the same
dependency-injection choice `cache.py` makes -- constructing a role-scoped connection from a URL
is a deployment concern for whichever entry point runs `leanserv` for real (not yet built), not
something this module should hardcode.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from lean_agent_core.blobs import store_or_inline, to_bytea
from lean_agent_core.enums import VerdictKind
from lean_agent_core.orm import Verdict
from lean_agent_core.protocols import BlobStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class VerdictInput:
    """Everything `write` needs to construct one `verdict` row (spec §5.3) -- one field per
    column except `created_at` (server-generated) and `cache_hit`'s default, which stays a plain
    `bool` here rather than optional since a caller always knows whether this verdict came from
    the cache.
    """

    attempt_id: uuid.UUID
    obligation_id: uuid.UUID
    kind: VerdictKind
    link_ok: bool
    replay_ok: bool
    axiom_audit_ok: bool
    elapsed_ms: int
    toolchain_rev: str
    mathlib_rev: str
    sealed_olean_sha_observed: bytes | None = None
    axioms: tuple[str, ...] | None = None
    kernels_agreeing: tuple[str, ...] | None = None
    messages: bytes | None = None
    infotree: bytes | None = None
    proof: bytes | None = None
    cache_hit: bool = False


class VerdictWriter:
    """`verdict.attempt_id` is the table's primary key (spec §5.3: at most one verdict per
    attempt, ever) -- `write` does not catch or paper over the resulting `IntegrityError` if
    called twice for the same attempt. A second verdict for one attempt is a caller bug (attempts
    are meant to be re-tried by creating a *new* attempt row, not by re-verifying the same one),
    and this module has no principled way to decide which of two conflicting verdicts should win.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], blob_store: BlobStore
    ) -> None:
        self._session_factory = session_factory
        self._blob_store = blob_store

    async def write(self, verdict: VerdictInput) -> None:
        messages_blob = (
            to_bytea(await store_or_inline(self._blob_store, verdict.messages, "text/plain"))
            if verdict.messages is not None
            else None
        )
        infotree_blob = (
            to_bytea(await store_or_inline(self._blob_store, verdict.infotree, "application/json"))
            if verdict.infotree is not None
            else None
        )
        proof_blob = (
            to_bytea(
                await store_or_inline(self._blob_store, verdict.proof, "application/octet-stream")
            )
            if verdict.proof is not None
            else None
        )

        row = Verdict(
            attempt_id=verdict.attempt_id,
            obligation_id=verdict.obligation_id,
            kind=verdict.kind,
            link_ok=verdict.link_ok,
            replay_ok=verdict.replay_ok,
            axiom_audit_ok=verdict.axiom_audit_ok,
            sealed_olean_sha_observed=verdict.sealed_olean_sha_observed,
            axioms=list(verdict.axioms) if verdict.axioms is not None else None,
            kernels_agreeing=list(verdict.kernels_agreeing)
            if verdict.kernels_agreeing is not None
            else None,
            messages_blob=messages_blob,
            infotree_blob=infotree_blob,
            proof_blob=proof_blob,
            elapsed_ms=verdict.elapsed_ms,
            cache_hit=verdict.cache_hit,
            toolchain_rev=verdict.toolchain_rev,
            mathlib_rev=verdict.mathlib_rev,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
