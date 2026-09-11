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

import json
import struct
from collections.abc import Sequence
from dataclasses import dataclass

from lean_agent_core.protocols import Completion, Exchange

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


def encode_completions(completions: tuple[Completion, ...]) -> bytes:
    """One request's samples as one blob.

    The arrays are packed (int32 / float32) and the envelope around them is JSON. That split is
    deliberate: the packing is where the volume is -- §6.5's 4 GB per 10⁹ tokens -- while the
    envelope is a handful of bytes per completion and buys a self-describing record that survives
    a schema change.

    **One encoder, two callers**: `model_response_cache` (M3.6) and `trajectory.token_ids_blob` /
    `logprobs_blob` (M3.7). If the cached form and the recorded form could drift, a trajectory
    replayed from the cache would not match the one it replayed -- so they cannot be separate
    functions that happen to agree today.
    """
    return json.dumps(
        [
            {
                "token_ids": pack_token_ids(c.token_ids).hex(),
                "logprobs": pack_logprobs(c.logprobs).hex(),
                "text": c.text,
                "finish_reason": c.finish_reason,
            }
            for c in completions
        ],
        separators=(",", ":"),
    ).encode()


def decode_completions(payload: bytes) -> tuple[Completion, ...]:
    try:
        entries = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CodecError(f"completions blob is not JSON: {exc}") from exc
    return tuple(
        Completion(
            token_ids=unpack_token_ids(bytes.fromhex(entry["token_ids"])),
            logprobs=unpack_logprobs(bytes.fromhex(entry["logprobs"])),
            text=entry["text"],
            finish_reason=entry["finish_reason"],
        )
        for entry in entries
    )


@dataclass(frozen=True)
class TokenExchange:
    """One exchange as read back from `token_ids_blob`: ids and what was asked, not the text."""

    prompt: tuple[int, ...]
    completions: tuple[tuple[int, ...], ...]
    sampling: dict[str, object]
    seed: int | None


def encode_trajectory_token_ids(exchanges: Sequence[Exchange]) -> bytes:
    """Spec §5.3's `token_ids_blob`: "prompt + completion token ids" -- per exchange.

    The prompt/completion boundary is kept, which is the only thing replay needs from this column:
    on-policy RL cannot compute a loss over tokens it cannot separate from the context they were
    conditioned on. And since M3.10 the boundary is kept *per request*: an attempt that asked twice
    (a repair after a failed sample) conditioned its second completions on a different prompt, and
    one prompt for all of them would pair them with the wrong context. Each exchange also carries
    its own sampling and seed, because a repair asks for one sample where the opening request
    asked for several; `trajectory.sampling`/`seed` hold the first exchange's.
    """
    return json.dumps(
        {
            "exchanges": [
                {
                    "prompt": pack_token_ids(exchange.prompt_token_ids).hex(),
                    "completions": [
                        pack_token_ids(c.token_ids).hex() for c in exchange.completions
                    ],
                    "sampling": exchange.sampling,
                    "seed": exchange.seed,
                }
                for exchange in exchanges
            ]
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def decode_trajectory_token_ids(payload: bytes) -> list[TokenExchange]:
    document = json.loads(payload)
    return [
        TokenExchange(
            prompt=unpack_token_ids(bytes.fromhex(entry["prompt"])),
            completions=tuple(unpack_token_ids(bytes.fromhex(ids)) for ids in entry["completions"]),
            sampling=dict(entry["sampling"]),
            seed=entry["seed"],
        )
        for entry in document["exchanges"]
    ]


def encode_trajectory_logprobs(exchanges: Sequence[Exchange]) -> bytes:
    """Spec §5.3's `logprobs_blob`: "sampled-token logprobs, float32", per exchange, parallel to
    `token_ids_blob`'s completions.

    Only the sampled tokens, never the prompt's: a prompt token has no sampled logprob, and §6.5
    is explicit that what is stored is "the sampled token's logprob ... not top-k".
    """
    return json.dumps(
        [[pack_logprobs(c.logprobs).hex() for c in exchange.completions] for exchange in exchanges],
        separators=(",", ":"),
    ).encode()


def decode_trajectory_logprobs(payload: bytes) -> list[list[tuple[float, ...]]]:
    return [
        [unpack_logprobs(bytes.fromhex(entry)) for entry in exchange]
        for exchange in json.loads(payload)
    ]
