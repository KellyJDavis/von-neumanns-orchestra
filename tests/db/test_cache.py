"""M1.8.4 exit criterion: `VerificationCacheStore` round-trips through both its tiers -- an
in-process (L0) hit never touches Postgres, and an L1 (Postgres `verification_cache`) hit
survives a fresh store instance with an empty L0 -- against a live PostgreSQL connected as the
real `leanserv` role (spec §5.5 grants it INSERT/UPDATE on `verification_cache`), never a mock.

Local dev / CI setup matches `test_privileges.py`: Postgres 16, migrations applied, then
`deploy/grants.sql`. `admin_engine`/`leanserv_async_database_url` come from `conftest.py`.

No pytest-asyncio, continuing the pattern from `tests/leanserv/`: plain sync `def test_...`
functions drive the async `VerificationCacheStore` API via `asyncio.run`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from lean_agent_core.blobs import LocalBlobStore
from lean_agent_core.enums import VerdictKind
from lean_agent_serv.cache import CachedCheck, VerificationCacheStore, compute_cache_key
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
def leanserv_sessionmaker(
    leanserv_async_database_url: str,
) -> Iterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(leanserv_async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _cleanup_verification_cache(admin_engine: Engine) -> Iterator[None]:
    yield
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM verification_cache"))
        conn.commit()


def test_compute_cache_key_is_deterministic_and_input_sensitive() -> None:
    key = compute_cache_key(b"base-env-digest", "def foo : Nat := 5", {"paranoid": False})
    assert key == compute_cache_key(b"base-env-digest", "def foo : Nat := 5", {"paranoid": False})
    assert key != compute_cache_key(b"different-digest", "def foo : Nat := 5", {"paranoid": False})
    assert key != compute_cache_key(b"base-env-digest", "def foo : Nat := 6", {"paranoid": False})
    assert key != compute_cache_key(b"base-env-digest", "def foo : Nat := 5", {"paranoid": True})


def test_compute_cache_key_options_order_does_not_matter() -> None:
    """`canonical(check_options)` must not depend on dict insertion order -- two logically
    identical option sets built in different orders are the same cache entry."""
    key_a = compute_cache_key(b"digest", "body", {"a": 1, "b": 2})
    key_b = compute_cache_key(b"digest", "body", {"b": 2, "a": 1})
    assert key_a == key_b


def test_get_returns_none_for_an_unknown_key(
    leanserv_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    store = VerificationCacheStore(leanserv_sessionmaker, LocalBlobStore(tmp_path))
    result = asyncio.run(store.get(b"never-put-this-key"))
    assert result is None


def test_put_then_get_round_trips_small_and_large_payloads(
    leanserv_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    store = VerificationCacheStore(leanserv_sessionmaker, LocalBlobStore(tmp_path))
    cache_key = compute_cache_key(b"digest", "theorem t : True := trivial", {})
    large_infotree = b"i" * (64 * 1024 + 1)  # above the M1.7 inline threshold -- exercises BlobRef
    result = CachedCheck(
        kind=VerdictKind.PROVED,
        axioms=("propext", "Classical.choice"),
        messages=b"no errors",
        infotree=large_infotree,
        elapsed_ms=123,
        toolchain_rev="v4.33.1",
        mathlib_rev="deadbeef",
    )

    async def run() -> CachedCheck | None:
        await store.put(cache_key, result)
        return await store.get(cache_key)

    fetched = asyncio.run(run())
    assert fetched == result


def test_l0_hit_never_touches_postgres(
    leanserv_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """After `put`, `get` for the same key must come back from the in-process L0 dict alone --
    proven by swapping in a session factory that points at a host that doesn't exist *before*
    calling `get`. `engine.dispose()` alone wouldn't prove this (SQLAlchemy engines transparently
    reconnect on next use), so the swap targets an address no reconnect attempt could ever reach.
    """
    store = VerificationCacheStore(leanserv_sessionmaker, LocalBlobStore(tmp_path))
    cache_key = compute_cache_key(b"digest", "def x : Nat := 1", {})
    result = CachedCheck(
        kind=VerdictKind.PROVED,
        axioms=None,
        messages=None,
        infotree=None,
        elapsed_ms=1,
        toolchain_rev="v4.33.1",
        mathlib_rev="deadbeef",
    )
    unreachable_engine = create_async_engine(
        "postgresql+asyncpg://leanserv:x@host.invalid:5432/leanagent"
    )

    async def run() -> CachedCheck | None:
        await store.put(cache_key, result)
        store._session_factory = async_sessionmaker(unreachable_engine, expire_on_commit=False)
        try:
            return await store.get(cache_key)
        finally:
            await unreachable_engine.dispose()

    assert asyncio.run(run()) == result


def test_l1_hit_survives_a_fresh_store_with_empty_l0(
    leanserv_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The whole point of L1: a *different* `VerificationCacheStore` (a different process, in
    production) with nothing in its own L0 still gets the cached result, straight from Postgres.

    Both stores' calls run inside one `asyncio.run` (not two separate ones): asyncpg connections
    are bound to the event loop that created them, so reusing the same engine/session factory
    across two separate `asyncio.run` calls in one test raises "Future attached to a different
    loop" -- confirmed empirically, not a hypothetical footgun.
    """
    writer_store = VerificationCacheStore(leanserv_sessionmaker, LocalBlobStore(tmp_path))
    reader_store = VerificationCacheStore(leanserv_sessionmaker, LocalBlobStore(tmp_path))
    cache_key = compute_cache_key(b"digest", "def y : Nat := 2", {})
    result = CachedCheck(
        kind=VerdictKind.ERRORS,
        axioms=None,
        messages=b"some error",
        infotree=None,
        elapsed_ms=7,
        toolchain_rev="v4.33.1",
        mathlib_rev="deadbeef",
    )

    async def run() -> CachedCheck | None:
        await writer_store.put(cache_key, result)
        return await reader_store.get(cache_key)

    assert asyncio.run(run()) == result


def test_repeated_put_for_the_same_key_is_idempotent(
    leanserv_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path, admin_engine: Engine
) -> None:
    """Two workers computing the same content-addressed key is expected, not an error -- the
    second `put` must not raise, and the row must not be duplicated."""
    store = VerificationCacheStore(leanserv_sessionmaker, LocalBlobStore(tmp_path))
    cache_key = compute_cache_key(b"digest", "def z : Nat := 3", {})
    result = CachedCheck(
        kind=VerdictKind.PROVED,
        axioms=None,
        messages=None,
        infotree=None,
        elapsed_ms=1,
        toolchain_rev="v4.33.1",
        mathlib_rev="deadbeef",
    )

    async def run() -> None:
        await store.put(cache_key, result)
        await store.put(cache_key, result)

    asyncio.run(run())

    with admin_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM verification_cache WHERE cache_key = :k"), {"k": cache_key}
        ).scalar_one()
    assert count == 1


def test_get_increments_hits_and_sets_last_hit_at(
    leanserv_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path, admin_engine: Engine
) -> None:
    cache_key = compute_cache_key(b"digest", "def w : Nat := 4", {})
    result = CachedCheck(
        kind=VerdictKind.PROVED,
        axioms=None,
        messages=None,
        infotree=None,
        elapsed_ms=1,
        toolchain_rev="v4.33.1",
        mathlib_rev="deadbeef",
    )

    async def run() -> None:
        # A fresh store per call so `get`'s L0 short-circuit doesn't hide the L1 hits update --
        # each `get` here must genuinely reach Postgres to prove the bookkeeping happens there.
        writer = VerificationCacheStore(leanserv_sessionmaker, LocalBlobStore(tmp_path))
        await writer.put(cache_key, result)
        await VerificationCacheStore(leanserv_sessionmaker, LocalBlobStore(tmp_path)).get(cache_key)
        await VerificationCacheStore(leanserv_sessionmaker, LocalBlobStore(tmp_path)).get(cache_key)

    asyncio.run(run())

    with admin_engine.connect() as conn:
        hits, last_hit_at = conn.execute(
            text("SELECT hits, last_hit_at FROM verification_cache WHERE cache_key = :k"),
            {"k": cache_key},
        ).one()
    assert hits == 2
    assert last_hit_at is not None
