"""The trajectory viewer's data (spec §7.4, M3.11): one attempt, exactly as it happened.

> A read-only trajectory viewer over `trajectory`, `tool_call`, and `verdict` showing exact
> rendered prompts (not reconstructions) is the primary debugging tool and must exist by MVP
> Phase 3, not at the end.

Each clause of that sentence is a decision here.

**"exact rendered prompts (not reconstructions)"** -- a prompt is shown by decoding the token ids
the trajectory stored, with the tokenizer whose digest the trajectory recorded. It is never
rebuilt by rendering the policy's messages through a template again: that would be a
reconstruction, and it would silently disagree with the record the day the template, the prompt
asset or the tokenizer moved -- exactly when someone is most likely to be debugging. When this
deployment does not have the recorded tokenizer, the viewer shows the ids and says so.

**"read-only"** -- enforced by the database, not by the absence of writes in this module. Every
query runs inside `SET TRANSACTION READ ONLY`, so a future edit that slipped a write in here would
fail at Postgres rather than quietly change the record it is displaying.

**"over `trajectory`, `tool_call`, and `verdict`"** -- all three, read in one transaction so the
view is of one consistent moment.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from lean_agent_core.blobs import from_bytea
from lean_agent_core.codecs import (
    TokenExchange,
    decode_trajectory_logprobs,
    decode_trajectory_token_ids,
)
from lean_agent_core.protocols import BlobStore, TokenDecoder, TokenizerResolver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lean_agent_api.schemas import (
    CompletionBody,
    ExchangeBody,
    PromptBody,
    StepBody,
    ToolCallBody,
    TrajectoryResponse,
    VerdictDetail,
)


@asynccontextmanager
async def read_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A session whose transaction Postgres refuses to write in.

    `SET TRANSACTION READ ONLY` must be the transaction's first statement, which it is: the session
    begins its transaction on this very `execute`. Rolled back on the way out -- there is nothing
    to commit, and a rollback cannot publish anything by accident.
    """
    async with session_factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield session
        finally:
            await session.rollback()


def _undecodable(revision: str | None, tokenizers: TokenizerResolver | None) -> str:
    if revision is None:
        return "the trajectory recorded no tokenizer revision; showing token ids"
    if tokenizers is None:
        return "this deployment has no tokenizer registry; showing token ids"
    return (
        f"tokenizer {revision} is not available to this deployment; showing token ids. The text is "
        "never reconstructed from the policy's messages (spec §7.4)."
    )


def _exchange(
    index: int,
    exchange: TokenExchange,
    logprobs: list[tuple[float, ...]],
    decoder: TokenDecoder | None,
    note: str,
    revision: str | None,
) -> ExchangeBody:
    prompt = PromptBody(
        token_count=len(exchange.prompt),
        text=decoder.decode(exchange.prompt) if decoder is not None else None,
        decoded_with=revision if decoder is not None else None,
        token_ids=None if decoder is not None else list(exchange.prompt),
        note=None if decoder is not None else note,
    )
    completions = [
        CompletionBody(
            index=i,
            token_count=len(ids),
            text=decoder.decode(ids) if decoder is not None else None,
            finish_reason=(
                exchange.finish_reasons[i] if i < len(exchange.finish_reasons) else None
            ),
            logprob_sum=float(sum(logprobs[i])) if i < len(logprobs) else 0.0,
            token_ids=None if decoder is not None else list(ids),
        )
        for i, ids in enumerate(exchange.completions)
    ]
    return ExchangeBody(
        index=index,
        sampling=exchange.sampling,
        seed=exchange.seed,
        prompt=prompt,
        completions=completions,
    )


async def _blob_text(blobs: BlobStore, value: Any) -> str | None:
    if value is None:
        return None
    return (await from_bytea(blobs, bytes(value))).decode("utf-8", errors="replace")


async def load_trajectory(
    session_factory: async_sessionmaker[AsyncSession],
    blobs: BlobStore,
    attempt_id: uuid.UUID,
    tokenizers: TokenizerResolver | None,
) -> TrajectoryResponse | None:
    """Everything recorded about one attempt, or `None` if it has no trajectory."""
    async with read_only(session_factory) as session:
        row = (
            await session.execute(
                text(
                    "SELECT provenance::text, model_id, model_weights_hash, tokenizer_revision, "
                    "sampling, seed, n_steps, steps_blob, token_ids_blob, logprobs_blob "
                    "FROM trajectory WHERE attempt_id = :id"
                ),
                {"id": attempt_id},
            )
        ).one_or_none()
        if row is None:
            return None
        verdict = (
            await session.execute(
                text(
                    "SELECT kind::text, link_ok, replay_ok, axiom_audit_ok, axioms, "
                    "kernels_agreeing, elapsed_ms, cache_hit, messages_blob, proof_blob "
                    "FROM verdict WHERE attempt_id = :id"
                ),
                {"id": attempt_id},
            )
        ).one_or_none()
        tool_rows = (
            await session.execute(
                text(
                    "SELECT step_index, server, tool, trust::text, ok, latency_ms, args_blob, "
                    "result_blob FROM tool_call WHERE attempt_id = :id ORDER BY step_index, id"
                ),
                {"id": attempt_id},
            )
        ).all()

    (
        provenance,
        model_id,
        weights,
        revision,
        sampling,
        seed,
        n_steps,
        steps_blob,
        tokens_blob,
        logprobs_blob,
    ) = row
    steps = [StepBody(**step) for step in json.loads(await from_bytea(blobs, bytes(steps_blob)))]

    exchanges: list[ExchangeBody] = []
    if tokens_blob is not None:
        decoded = decode_trajectory_token_ids(await from_bytea(blobs, bytes(tokens_blob)))
        logprobs = (
            decode_trajectory_logprobs(await from_bytea(blobs, bytes(logprobs_blob)))
            if logprobs_blob is not None
            else []
        )
        decoder = tokenizers(revision) if tokenizers is not None and revision else None
        note = _undecodable(revision, tokenizers)
        exchanges = [
            _exchange(i, e, logprobs[i] if i < len(logprobs) else [], decoder, note, revision)
            for i, e in enumerate(decoded)
        ]

    detail: VerdictDetail | None = None
    if verdict is not None:
        messages = await _blob_text(blobs, verdict[8])
        detail = VerdictDetail(
            kind=verdict[0],
            link_ok=verdict[1],
            replay_ok=verdict[2],
            axiom_audit_ok=verdict[3],
            axioms=list(verdict[4] or []),
            kernels_agreeing=list(verdict[5] or []),
            elapsed_ms=verdict[6],
            cache_hit=verdict[7],
            diagnostics=list(json.loads(messages)) if messages else [],
            proof=await _blob_text(blobs, verdict[9]),
        )

    return TrajectoryResponse(
        attempt_id=attempt_id,
        provenance=provenance,
        model_id=model_id,
        model_weights_hash=weights,
        tokenizer_revision=revision,
        sampling=dict(sampling or {}),
        seed=seed,
        n_steps=n_steps,
        steps=steps,
        exchanges=exchanges,
        tool_calls=[
            ToolCallBody(
                step_index=t[0],
                server=t[1],
                tool=t[2],
                trust=t[3],
                ok=t[4],
                latency_ms=t[5],
                args=await _blob_text(blobs, t[6]) or "",
                result=await _blob_text(blobs, t[7]),
            )
            for t in tool_rows
        ],
        verdict=detail,
    )
