"""The response cache (spec §6.5).

> Response cache keyed on `sha256(prompt_tokens) ‖ model_id ‖ canonical(sampling) ‖ seed`.

**Not everything that has a key may be cached, and that is the important part of this module.**
Sampling at a nonzero temperature with no seed is a request for *fresh* randomness; two such calls
share a cache key and must not share an answer. Serving the second from cache would turn
`WholeProofSampler`'s "sample n proofs and check them all" into one proof checked n times, and the
pass rate would move for a reason nothing in the trajectory would show. `is_cacheable` refuses
those, and the store never writes them.

Seeded sampling and greedy decoding (`temperature == 0`) are both fine: the server has been asked
for a reproducible answer, so returning the one it already gave is what the caller asked for.

L1 only -- a Postgres table, shared across workers and runs. `VerificationCacheStore` has an
in-process L0 in front of its L1 because a Lean check genuinely is repeated within one process
during a portfolio sweep; a model request repeated bit-for-bit inside one process is rare enough
that the tier would be surface area without a caller. Adding it later costs nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from lean_agent_core.blobs import from_bytea, store_or_inline, to_bytea
from lean_agent_core.codecs import pack_logprobs, pack_token_ids, unpack_logprobs, unpack_token_ids
from lean_agent_core.protocols import (
    BlobStore,
    Completion,
    CompletionResponse,
    SamplingParams,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def compute_response_cache_key(
    prompt_token_ids: tuple[int, ...],
    model_id: str,
    sampling: SamplingParams,
    seed: int | None,
) -> bytes:
    """Spec §6.5's key, with `canonical(...)` meaning sorted-key JSON as it does in §5.3.

    The prompt is hashed rather than concatenated raw, per spec's own `sha256(prompt_tokens)`, and
    the *packed* bytes are what is hashed -- so the key depends on the token ids themselves rather
    than on how some caller happened to render them.
    """
    prompt_digest = hashlib.sha256(pack_token_ids(prompt_token_ids)).digest()
    canonical = json.dumps(sampling.canonical(), sort_keys=True, separators=(",", ":")).encode()
    seed_bytes = b"none" if seed is None else str(seed).encode()
    return hashlib.sha256(prompt_digest + model_id.encode() + canonical + seed_bytes).digest()


def is_cacheable(sampling: SamplingParams, seed: int | None) -> bool:
    """Whether a response to this request may be reused.

    False for unseeded sampling at a nonzero temperature: the caller asked for fresh randomness,
    and two such requests share a key. Returning the stored answer would silently convert
    resampling into repetition -- `WholeProofSampler` would check one proof n times and report a
    pass rate for an experiment nobody ran.

    True at `temperature == 0` even without a seed, because greedy decoding is already the
    request for a reproducible answer. (Batching can still perturb it in practice; spec §7.2 puts
    that under R1 and is explicit that token-identity is "a verification affordance, not a
    mechanism anything depends on".)
    """
    return seed is not None or sampling.temperature == 0.0


@dataclass(frozen=True)
class CachedResponse:
    """The payload columns of a `model_response_cache` row, minus the bookkeeping the store keeps
    on the caller's behalf (`hits`, `created_at`, `last_hit_at`)."""

    model_id: str
    completions: tuple[Completion, ...]
    prompt_token_ids: tuple[int, ...]
    elapsed_ms: int
    weights_revision: str | None = None
    tokenizer_revision: str | None = None


def _encode_completions(completions: tuple[Completion, ...]) -> bytes:
    """One request's samples as one blob.

    Token ids and logprobs go through `lean_agent_core.codecs` (int32 / float32) rather than into
    JSON, because §6.5 sized the corpus on 4 bytes per logprob and JSON costs three to five times
    that. The surrounding structure is JSON, with the arrays hex-encoded inside it -- the packing
    is where the volume is, and a self-describing envelope is worth more than the bytes it costs.
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


def _decode_completions(payload: bytes) -> tuple[Completion, ...]:
    return tuple(
        Completion(
            token_ids=unpack_token_ids(bytes.fromhex(entry["token_ids"])),
            logprobs=unpack_logprobs(bytes.fromhex(entry["logprobs"])),
            text=entry["text"],
            finish_reason=entry["finish_reason"],
        )
        for entry in json.loads(payload)
    )


class ResponseCacheStore:
    """`model_response_cache`, read and written as the `app` role.

    Written by `app` rather than `leanserv`, unlike `verification_cache`: that table records what
    the *kernel* said, so only the service running the kernel may write it. A model completion is
    the agent's own work, cached to avoid paying for it twice.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], blob_store: BlobStore
    ) -> None:
        self._sessions = session_factory
        self._blobs = blob_store

    async def get(self, cache_key: bytes) -> CachedResponse | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE model_response_cache SET hits = hits + 1, last_hit_at = now() "
                        "WHERE cache_key = :k RETURNING model_id, weights_revision, "
                        "tokenizer_revision, prompt_token_ids_blob, completions_blob, elapsed_ms"
                    ),
                    {"k": cache_key},
                )
            ).one_or_none()
            await session.commit()

        if row is None:
            return None

        model_id, weights_revision, tokenizer_revision, prompt_blob, completions_blob, elapsed = row
        prompt_token_ids: tuple[int, ...] = ()
        if prompt_blob is not None:
            prompt_token_ids = unpack_token_ids(await from_bytea(self._blobs, bytes(prompt_blob)))
        return CachedResponse(
            model_id=model_id,
            completions=_decode_completions(await from_bytea(self._blobs, bytes(completions_blob))),
            prompt_token_ids=prompt_token_ids,
            elapsed_ms=int(elapsed),
            weights_revision=weights_revision,
            tokenizer_revision=tokenizer_revision,
        )

    async def put(self, cache_key: bytes, response: CompletionResponse) -> None:
        """Store one request's samples.

        `ON CONFLICT DO NOTHING`, like `VerificationCacheStore.put` and for the same reason: two
        workers computing the same content-addressed key concurrently is the expected case, not a
        race to detect. The first answer stored wins, and both are answers to the same question.
        """
        completions = to_bytea(
            await store_or_inline(
                self._blobs, _encode_completions(response.completions), "application/json"
            )
        )
        prompt = to_bytea(
            await store_or_inline(
                self._blobs,
                pack_token_ids(response.prompt_token_ids),
                "application/octet-stream",
            )
        )
        async with self._sessions() as session:
            await session.execute(
                text(
                    "INSERT INTO model_response_cache (cache_key, model_id, weights_revision, "
                    "tokenizer_revision, prompt_token_ids_blob, completions_blob, n_completions, "
                    "elapsed_ms) VALUES (:k, :m, :w, :t, :p, :c, :n, :e) "
                    "ON CONFLICT (cache_key) DO NOTHING"
                ),
                {
                    "k": cache_key,
                    "m": response.model_id,
                    "w": response.model_weights_hash,
                    "t": response.tokenizer_revision,
                    "p": prompt,
                    "c": completions,
                    "n": len(response.completions),
                    "e": response.elapsed_ms,
                },
            )
            await session.commit()
