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
