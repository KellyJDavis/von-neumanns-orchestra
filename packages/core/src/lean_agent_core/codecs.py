"""Packing token ids and logprobs for storage (spec §6.5).

> Request `logprobs` on every sampled token and store them. Recomputing behavior-policy logprobs
> later produces the train/inference mismatch. **Store the sampled token's logprob as float32 --
> about 4 GB per 10⁹ tokens** -- not top-k.

That figure is arithmetic that only comes out one way: 4 bytes × 10⁹. So the storage format is
float32, and JSON is not an option -- a logprob rendered as text costs 10-20 bytes, which is three
to five times the budget spec sized the corpus against. The same reasoning applies to token ids,
where a decimal rendering averages worse than a fixed four bytes and never better.

Two consumers, one codec: `model_response_cache` (M3.6) and `trajectory.token_ids_blob` /
`logprobs_blob` (§5.3, M3.7). Written once here so the cached form and the recorded form cannot
drift -- if they did, a trajectory replayed from the cache would not match the one it replayed.
"""

from __future__ import annotations

import struct

#: Little-endian, fixed width, no padding. Explicit rather than native (`=`/`@`) because these
#: bytes are written to a database that a different machine will read: native byte order would make
#: a corpus unreadable on a big-endian host, and native alignment would insert padding that the
#: length arithmetic below does not expect.
_TOKEN_FORMAT = "<i"
_LOGPROB_FORMAT = "<f"

TOKEN_BYTES = struct.calcsize(_TOKEN_FORMAT)
LOGPROB_BYTES = struct.calcsize(_LOGPROB_FORMAT)


class CodecError(ValueError):
    """Bytes that are not a valid packing. Raised rather than truncated: a partially-decoded
    logprob array would be silently shorter than its token ids, and the pair being parallel is the
    whole reason to store them."""


def pack_token_ids(token_ids: tuple[int, ...]) -> bytes:
    """Signed int32, which covers every vocabulary in use and leaves room.

    Signed rather than unsigned so an out-of-range value fails here instead of wrapping into a
    plausible-looking token id -- `struct` raises on a negative under `<I`, but a caller passing
    `-1` as a sentinel would otherwise get token 4294967295 back.
    """
    try:
        return struct.pack(f"<{len(token_ids)}i", *token_ids)
    except struct.error as exc:
        raise CodecError(f"token ids do not fit in int32: {exc}") from exc


def unpack_token_ids(data: bytes) -> tuple[int, ...]:
    if len(data) % TOKEN_BYTES:
        raise CodecError(
            f"{len(data)} bytes is not a whole number of int32 token ids "
            f"({len(data) % TOKEN_BYTES} left over)"
        )
    return struct.unpack(f"<{len(data) // TOKEN_BYTES}i", data)


def pack_logprobs(logprobs: tuple[float, ...]) -> bytes:
    """float32, per spec's own storage budget.

    Precision loss is intended, not tolerated: a logprob is the output of a sampler that already
    carries far more uncertainty than float32's ~7 significant digits, and spec sized the corpus
    on 4 bytes each. What matters is that the loss is *deterministic*, so packing the same value
    twice gives the same bytes and a cache key over them is stable.
    """
    try:
        return struct.pack(f"<{len(logprobs)}f", *logprobs)
    except (struct.error, OverflowError) as exc:
        raise CodecError(f"logprobs do not fit in float32: {exc}") from exc


def unpack_logprobs(data: bytes) -> tuple[float, ...]:
    if len(data) % LOGPROB_BYTES:
        raise CodecError(
            f"{len(data)} bytes is not a whole number of float32 logprobs "
            f"({len(data) % LOGPROB_BYTES} left over)"
        )
    return struct.unpack(f"<{len(data) // LOGPROB_BYTES}f", data)


def round_trip_logprobs(logprobs: tuple[float, ...]) -> tuple[float, ...]:
    """What `logprobs` becomes once stored. Useful to a caller that wants to compare a live value
    against a stored one without asking which side lost precision."""
    return unpack_logprobs(pack_logprobs(logprobs))
