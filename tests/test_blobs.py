"""M1.7 exit criterion: put/get/exists round-trips correctly on both sides of the 64 KiB
inline-vs-CAS threshold (spec §5.3). Pure filesystem -- no Postgres needed, so this lives
under `tests/` rather than `tests/db/`.

No pytest-asyncio dependency: the workspace has no async test infra yet (`tests/db/` tests the
async-capable ORM through a sync driver/session), so these tests just drive the async
`LocalBlobStore`/`store_or_inline` API with `asyncio.run` from plain sync test functions rather
than adding a new dependency for one file.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from lean_agent_core.blobs import (
    INLINE_THRESHOLD,
    BlobRef,
    Inline,
    LocalBlobStore,
    from_bytea,
    store_or_inline,
    to_bytea,
)


def test_put_get_exists_round_trip_small_and_large(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    small = b"hello world"
    large = b"x" * (INLINE_THRESHOLD * 2)

    async def run() -> None:
        for data in (small, large):
            assert not await store.exists(hashlib.sha256(data).digest())
            digest = await store.put(data, "application/octet-stream")
            assert digest == hashlib.sha256(data).digest()
            assert await store.exists(digest)
            assert await store.get(digest) == data

    asyncio.run(run())


def test_put_is_sharded_and_content_addressed(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    data = b"shard me"
    digest = asyncio.run(store.put(data, "text/plain"))

    hex_digest = digest.hex()
    expected_path = tmp_path / hex_digest[:2] / hex_digest
    assert expected_path.read_bytes() == data


def test_put_is_idempotent_for_identical_content(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    data = b"same content twice"

    async def run() -> tuple[bytes, bytes]:
        first = await store.put(data, "text/plain")
        second = await store.put(data, "text/plain")
        return first, second

    first, second = asyncio.run(run())
    assert first == second
    assert asyncio.run(store.get(first)) == data


def test_url_points_at_the_sharded_path(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    digest = asyncio.run(store.put(b"url me", "text/plain"))
    hex_digest = digest.hex()
    assert store.url(digest) == f"file://{tmp_path / hex_digest[:2] / hex_digest}"


def test_store_or_inline_at_or_under_threshold_returns_inline(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    at_threshold = b"y" * INLINE_THRESHOLD

    result = asyncio.run(store_or_inline(store, at_threshold, "text/plain"))

    assert isinstance(result, Inline)
    assert result.data == at_threshold


def test_store_or_inline_above_threshold_returns_blob_ref_and_writes_through(
    tmp_path: Path,
) -> None:
    store = LocalBlobStore(tmp_path)
    above_threshold = b"z" * (INLINE_THRESHOLD + 1)

    result = asyncio.run(store_or_inline(store, above_threshold, "text/plain"))

    assert isinstance(result, BlobRef)
    assert result.digest == hashlib.sha256(above_threshold).digest()


def test_bytea_round_trips_inline_content(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    data = b"small enough to inline"

    async def run() -> bytes:
        routed = await store_or_inline(store, data, "text/plain")
        column_value = to_bytea(routed)
        return await from_bytea(store, column_value)

    assert asyncio.run(run()) == data


def test_bytea_round_trips_blob_ref_content(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    data = b"y" * (INLINE_THRESHOLD + 1)

    async def run() -> bytes:
        routed = await store_or_inline(store, data, "text/plain")
        column_value = to_bytea(routed)
        return await from_bytea(store, column_value)

    assert asyncio.run(run()) == data


def test_bytea_inline_and_blob_ref_encodings_are_never_ambiguous(tmp_path: Path) -> None:
    """The whole reason `to_bytea` tags its output: an inline value that happens to be exactly a
    digest's length (32 bytes) must still decode as itself, not be mistaken for a `BlobRef` --
    without the tag, this is exactly the ambiguity a bare `bytea` column can't resolve.
    """
    store = LocalBlobStore(tmp_path)
    thirty_two_bytes = b"x" * 32

    column_value = to_bytea(Inline(thirty_two_bytes))

    assert asyncio.run(from_bytea(store, column_value)) == thirty_two_bytes


def test_from_bytea_rejects_an_unrecognized_tag(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)

    with pytest.raises(ValueError, match="unrecognized"):
        asyncio.run(from_bytea(store, b"\x02not a tag this module ever wrote"))
