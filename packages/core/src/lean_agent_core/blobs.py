"""Content-addressed blob store (spec §2 "local CAS → S3-compatible", §3, Appendix A's
`BlobStore` protocol) and the inline-vs-CAS routing policy from spec §5.3: "Anything above 64
KiB [measure] goes to the CAS, never to Postgres. Rows hold `bytea` digests only."

`LocalBlobStore` is the local-filesystem backend for MVP -- the spec's own staged path is local
CAS now, S3-compatible later; nothing here assumes local-only, but nothing here implements S3
either, since there's no caller yet that would need it. `put`/`get`/`exists` don't touch
Postgres or the `blob` table at all: the protocol they implement (Appendix A) takes no session
and no `tenant_id`, so tenant-scoped visibility bookkeeping is a concern for whatever higher-level
caller creates a `blob` row (e.g. an eventual `/v1/blobs` upload endpoint), not for the store
itself.

`store_or_inline` decides *which* of inline-or-CAS a value gets; `to_bytea`/`from_bytea` (added
in M1.8.4, the first real caller) are the encode/decode pair that makes writing and reading a
blob-suffixed column actually round-trip -- see their own docstrings for why a bare `bytea`
column can't tell the two cases apart on its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import NamedTuple

from lean_agent_core.protocols import BlobStore

INLINE_THRESHOLD = 64 * 1024  # 64 KiB (spec §5.3, marked [measure] -- not yet tuned)


class LocalBlobStore:
    """Content-addressed store on the local filesystem, sharded by the first two hex digits of
    each digest (the same convention as Git's own object store) so a single directory never
    holds an unbounded number of entries.

    File I/O is synchronous underneath (`asyncio.to_thread`), not `aiofiles`: local-disk reads
    and writes are fast enough in practice that a dedicated async-file library isn't worth a new
    dependency for this, and the public interface stays `async` either way to match the
    `BlobStore` protocol.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path_for(self, digest: bytes) -> Path:
        hex_digest = digest.hex()
        return self._root / hex_digest[:2] / hex_digest

    async def put(self, data: bytes, media_type: str) -> bytes:
        del media_type  # not needed to place the content; recorded by a `blob` row, not here
        digest = hashlib.sha256(data).digest()
        await asyncio.to_thread(self._write, digest, data)
        return digest

    def _write(self, digest: bytes, data: bytes) -> None:
        path = self._path_for(digest)
        if path.exists():
            # Content-addressed: identical content always maps to this exact path, so an
            # existing file is assumed already correct and rewriting it is skipped.
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-to-temp-then-rename: rename is atomic on POSIX, so a process crash mid-write
        # never leaves a partially-written file sitting at the final path looking valid.
        tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        tmp_path.write_bytes(data)
        tmp_path.replace(path)

    async def get(self, digest: bytes) -> bytes:
        return await asyncio.to_thread(self._path_for(digest).read_bytes)

    async def exists(self, digest: bytes) -> bool:
        return await asyncio.to_thread(self._path_for(digest).exists)

    def url(self, digest: bytes) -> str:
        return f"file://{self._path_for(digest)}"


class Inline(NamedTuple):
    """Small enough to store directly in a `bytea` column."""

    data: bytes


class BlobRef(NamedTuple):
    """Too large to inline (spec §5.3): the content lives in a `BlobStore`, keyed by this
    digest. The column holds `digest`, not the content."""

    digest: bytes


async def store_or_inline(
    store: BlobStore, data: bytes, media_type: str, *, threshold: int = INLINE_THRESHOLD
) -> Inline | BlobRef:
    """Apply spec §5.3's routing rule: `data` at or under `threshold` comes back as `Inline`
    (the caller writes `data` itself into its `bytea` column); above it, `data` is written to
    `store` and the caller writes the returned digest into the column instead. `Inline` and
    `BlobRef` are distinct `NamedTuple` types specifically so a caller can `isinstance`-check
    which one it got -- both ultimately wrap plain `bytes`, which would otherwise be ambiguous.
    """
    if len(data) <= threshold:
        return Inline(data)
    digest = await store.put(data, media_type)
    return BlobRef(digest)


# A blob-suffixed `bytea` column (`verdict.messages_blob`, `verification_cache.messages_blob`,
# `obligation.proof_blob`, ...) holds *either* inline content or a CAS digest depending on size --
# but the column itself is just `bytea`, with nothing else recording which case a given row is.
# `Inline`/`BlobRef` disambiguate this in memory, right after `store_or_inline` runs, but that
# distinction is lost the moment either gets written to the same untyped column -- a 32-byte
# inline value would be indistinguishable from a digest by length alone. `to_bytea`/`from_bytea`
# fix this with a one-byte tag prefixed onto the column's actual bytes, so the encoding is
# self-describing without needing a schema change (a second column, or widening the digest to a
# fixed recognizable length) for something this cheap to solve entirely within the existing type.
_INLINE_TAG = b"\x00"
_BLOB_REF_TAG = b"\x01"


def to_bytea(routed: Inline | BlobRef) -> bytes:
    """Serialize a `store_or_inline` result into the actual bytes a blob-suffixed column should
    store. The one-byte tag this prepends is why a column populated this way must always be read
    back through `from_bytea`, never compared or used directly -- see the module-level note above
    on why the tag exists at all.
    """
    if isinstance(routed, Inline):
        return _INLINE_TAG + routed.data
    return _BLOB_REF_TAG + routed.digest


async def from_bytea(store: BlobStore, column_value: bytes) -> bytes:
    """Inverse of `to_bytea`: recover the original content from a blob-suffixed column's stored
    bytes, following the digest through `store` if `to_bytea` didn't inline it. Raises `ValueError`
    on a tag byte this module never wrote -- there is no way to safely guess.
    """
    tag, payload = column_value[:1], column_value[1:]
    if tag == _INLINE_TAG:
        return payload
    if tag == _BLOB_REF_TAG:
        return await store.get(payload)
    raise ValueError(f"unrecognized blob-column tag byte {tag!r}")
