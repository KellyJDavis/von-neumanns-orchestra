"""Protocols shared across `lean_agent_core` and its consumers (spec Appendix A).

Only `BlobStore` exists so far -- it's the one M1.7 implements. `LeanService`, `ModelBackend`,
`Policy`, `ToolClient`, and `Sink` belong here too eventually (this is the file the repo layout
names as their home), but adding them now, with nothing to implement or consume them, would just
be speculative surface area with no way to know yet whether the shape is right.
"""

from typing import Protocol


class BlobStore(Protocol):
    async def put(self, data: bytes, media_type: str) -> bytes: ...
    async def get(self, digest: bytes) -> bytes: ...
    async def exists(self, digest: bytes) -> bool: ...
    def url(self, digest: bytes) -> str: ...
