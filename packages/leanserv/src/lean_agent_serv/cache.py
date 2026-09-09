"""Verification cache, L0 (in-process) and L1 (Postgres `verification_cache`, spec §5.3) tiers.

Spec's repo-layout comment names `cache.py` as owning "L0/L1/L2/L3 tiers"; L2 is `pool.py`'s warm-
worker reuse (M1.8.3 -- a "hit" there means an already-warm process was available, not that a
verdict was memoized) and L3 is pickled environment snapshots, explicitly deferred post-MVP (spec
§8: "L3 snapshot persistence: warm sealing needs a warm worker, not a persisted snapshot"). This
module is the other two: L0 is a small in-process dict so a request this exact process already
served doesn't even pay a database round trip; L1 is the real, global, cross-process cache spec
describes -- content-addressed, so a hit implies the requester already holds the content that
produced it (spec §7.2's residual timing-channel note about the cache being deliberately global).

`get`/`put` operate on already-decoded `messages`/`infotree` bytes, never on the raw column
values `to_bytea`/`from_bytea` (M1.7/M1.8.4, `lean_agent_core.blobs`) produce and consume --
callers of this module should never need to know that a `messages_blob` column can hold either
inline content or a CAS digest.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from lean_agent_core.blobs import from_bytea, store_or_inline, to_bytea
from lean_agent_core.enums import VerdictKind
from lean_agent_core.orm import VerificationCache
from lean_agent_core.protocols import BlobStore
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import func


def compute_cache_key(
    base_env_digest: bytes, declaration_source: str, options: dict[str, Any] | None = None
) -> bytes:
    """Spec §5.3: `cache_key = sha256(base_env_digest ‖ sha256(declaration_source) ‖
    canonical(check_options))`.

    `options` is a plain JSON-able mapping, not a `CheckOptions` model -- no such type exists yet
    in this codebase (`ReplWorker.check` takes only a body and a timeout; M1.8.1/M1.8.2 deliberately
    left `CheckOptions` unbuilt with nothing to vary yet). `canonical` here means
    `json.dumps(..., sort_keys=True)`: deterministic across calls with the same logical content
    regardless of key insertion order, which is all "canonical" needs to guarantee for a cache key.
    Extend this to take a real options type together with whatever first gives `check` request
    options worth varying the cache key on.
    """
    canonical_options = json.dumps(options or {}, sort_keys=True, separators=(",", ":")).encode()
    declaration_digest = hashlib.sha256(declaration_source.encode()).digest()
    return hashlib.sha256(base_env_digest + declaration_digest + canonical_options).digest()


@dataclass(frozen=True)
class CachedCheck:
    """The payload columns of a `verification_cache` row -- everything except `cache_key` itself
    and the bookkeeping columns (`hits`, `created_at`, `last_hit_at`) `VerificationCacheStore`
    manages on the caller's behalf.
    """

    kind: VerdictKind
    axioms: tuple[str, ...] | None
    messages: bytes | None
    infotree: bytes | None
    elapsed_ms: int
    toolchain_rev: str
    mathlib_rev: str


class VerificationCacheStore:
    """L0 (bounded in-process dict) in front of L1 (`verification_cache`, shared across every
    process per spec §7.2 -- deliberately global, not tenant-scoped, since a hit is a pure
    function of content-addressed inputs).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        blob_store: BlobStore,
        *,
        l0_capacity: int = 4096,
    ) -> None:
        self._session_factory = session_factory
        self._blob_store = blob_store
        self._l0_capacity = l0_capacity
        self._l0: OrderedDict[bytes, CachedCheck] = OrderedDict()

    async def get(self, cache_key: bytes) -> CachedCheck | None:
        if cache_key in self._l0:
            self._l0.move_to_end(cache_key)
            return self._l0[cache_key]

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(VerificationCache).where(VerificationCache.cache_key == cache_key)
                )
            ).scalar_one_or_none()
            if row is None:
                return None

            await session.execute(
                update(VerificationCache)
                .where(VerificationCache.cache_key == cache_key)
                .values(hits=VerificationCache.hits + 1, last_hit_at=func.now())
            )
            await session.commit()

        result = CachedCheck(
            kind=row.kind,
            axioms=tuple(row.axioms) if row.axioms is not None else None,
            messages=await from_bytea(self._blob_store, row.messages_blob)
            if row.messages_blob is not None
            else None,
            infotree=await from_bytea(self._blob_store, row.infotree_blob)
            if row.infotree_blob is not None
            else None,
            elapsed_ms=row.elapsed_ms,
            toolchain_rev=row.toolchain_rev,
            mathlib_rev=row.mathlib_rev,
        )
        self._l0_put(cache_key, result)
        return result

    async def put(self, cache_key: bytes, result: CachedCheck) -> None:
        """Idempotent: two workers computing the same content-addressed key concurrently both
        try to insert the identical row, so a conflict is expected and harmless -- the second
        writer's insert is simply discarded rather than erroring or overwriting.
        """
        messages_blob = (
            to_bytea(await store_or_inline(self._blob_store, result.messages, "text/plain"))
            if result.messages is not None
            else None
        )
        infotree_blob = (
            to_bytea(await store_or_inline(self._blob_store, result.infotree, "application/json"))
            if result.infotree is not None
            else None
        )

        async with self._session_factory() as session:
            await session.execute(
                pg_insert(VerificationCache)
                .values(
                    cache_key=cache_key,
                    kind=result.kind,
                    axioms=list(result.axioms) if result.axioms is not None else None,
                    messages_blob=messages_blob,
                    infotree_blob=infotree_blob,
                    elapsed_ms=result.elapsed_ms,
                    toolchain_rev=result.toolchain_rev,
                    mathlib_rev=result.mathlib_rev,
                )
                .on_conflict_do_nothing(index_elements=["cache_key"])
            )
            await session.commit()

        self._l0_put(cache_key, result)

    def _l0_put(self, cache_key: bytes, result: CachedCheck) -> None:
        self._l0[cache_key] = result
        self._l0.move_to_end(cache_key)
        if len(self._l0) > self._l0_capacity:
            self._l0.popitem(last=False)
