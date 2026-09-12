"""The training-corpus exporter, and the one rule it exists to enforce (spec §7.1).

"The corpus exporter **raises** on any trajectory outside `{open_weights, symbolic, human}`,
listing offending ids. It does not filter." The binding constraint on open weights is the
provenance of the training data, and a filter is the wrong shape for that rule: it would hand back
a corpus that *looks* clean while silently dropping whatever a run mixed in, and whoever widened
the selection next would never see what had been left out. So every selected trajectory's
provenance is read first, and a single one outside the set stops the export before any record is
built -- or written: `write_corpus` assembles everything before it opens the file, so a refused
export leaves no partial file to be mistaken for a clean one.

What makes the check mean something is upstream of it: `trajectory.provenance` is `NOT NULL` with
no default and is derived from the backend that served the completions, never asserted by whoever
wrote the row (M2.5, M3.7). The exporter trusts the column because nothing could fill it with a
convenient value.

Records carry token ids, never text. The ids are what the model saw and produced; text
re-tokenized later is exactly what §6.5 forbids, and it would not round-trip.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lean_agent_core.blobs import from_bytea
from lean_agent_core.codecs import decode_trajectory_logprobs, decode_trajectory_token_ids
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.protocols import BlobStore

#: Spec §7.1's set. `closed_api_eval_only` is outside it by name: a closed model's outputs may be
#: *evaluated* against, never trained on -- distilling one produces encumbered weights.
EXPORTABLE: frozenset[ProvenanceClass] = frozenset(
    {ProvenanceClass.OPEN_WEIGHTS, ProvenanceClass.SYMBOLIC, ProvenanceClass.HUMAN}
)


class MixedProvenanceError(Exception):
    """Some selected trajectories may not be exported.

    `offending` maps each attempt id to its provenance, so the message says exactly what has to
    leave the selection -- the "listing offending ids" half of §7.1, which a bare refusal would not
    satisfy.
    """

    def __init__(self, offending: Mapping[uuid.UUID, str]) -> None:
        self.offending = dict(sorted(offending.items(), key=lambda item: str(item[0])))
        allowed = sorted(p.value for p in EXPORTABLE)
        listed = ", ".join(
            f"{attempt} ({provenance})" for attempt, provenance in self.offending.items()
        )
        super().__init__(
            f"{len(self.offending)} trajectory(ies) outside {allowed}; nothing exported: {listed}"
        )


@dataclass(frozen=True)
class CorpusExchange:
    """One request of an attempt: the prompt it sent and every completion, with the sampled tokens'
    logprobs and why each ended. Per request because a repair's answers are conditioned on a
    different prompt from the opening request's (M3.10)."""

    prompt_token_ids: tuple[int, ...]
    completion_token_ids: tuple[tuple[int, ...], ...]
    completion_logprobs: tuple[tuple[float, ...], ...]
    finish_reasons: tuple[str, ...]
    sampling: dict[str, object]
    seed: int | None


@dataclass(frozen=True)
class CorpusRecord:
    """One attempt's trajectory as training data, with the tokenizer and weights its ids belong to
    and the verdict that says whether it proved anything."""

    attempt_id: uuid.UUID
    run_id: uuid.UUID
    obligation_id: uuid.UUID
    provenance: str
    model_id: str | None
    model_weights_hash: str | None
    tokenizer_revision: str | None
    verdict: str | None
    exchanges: tuple[CorpusExchange, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "attempt_id": str(self.attempt_id),
            "run_id": str(self.run_id),
            "obligation_id": str(self.obligation_id),
            "provenance": self.provenance,
            "model_id": self.model_id,
            "model_weights_hash": self.model_weights_hash,
            "tokenizer_revision": self.tokenizer_revision,
            "verdict": self.verdict,
            "exchanges": [
                {
                    "prompt_token_ids": list(e.prompt_token_ids),
                    "completion_token_ids": [list(c) for c in e.completion_token_ids],
                    "completion_logprobs": [list(lp) for lp in e.completion_logprobs],
                    "finish_reasons": list(e.finish_reasons),
                    "sampling": e.sampling,
                    "seed": e.seed,
                }
                for e in self.exchanges
            ],
        }


_SELECT = text(
    "SELECT t.attempt_id, a.run_id, a.obligation_id, t.provenance::text, t.model_id, "
    "t.model_weights_hash, t.tokenizer_revision, t.token_ids_blob, t.logprobs_blob, v.kind::text "
    "FROM trajectory t JOIN attempt a ON a.id = t.attempt_id "
    "LEFT JOIN verdict v ON v.attempt_id = t.attempt_id "
    "WHERE a.run_id = ANY(CAST(:runs AS uuid[])) "
    "ORDER BY a.run_id, t.created_at, t.attempt_id"
)


async def export_corpus(
    session_factory: async_sessionmaker[AsyncSession],
    blobs: BlobStore,
    run_ids: Sequence[uuid.UUID],
) -> list[CorpusRecord]:
    """Every trajectory of `run_ids`, or `MixedProvenanceError` naming the ones that may not go."""
    async with session_factory() as session:
        # Read-only by construction, as the trajectory viewer is (M3.11): an exporter has no
        # business writing, and the database is what says so.
        await session.execute(text("SET TRANSACTION READ ONLY"))
        rows = (await session.execute(_SELECT, {"runs": [str(r) for r in run_ids]})).all()
        await session.rollback()

    offending = {row[0]: row[3] for row in rows if ProvenanceClass(row[3]) not in EXPORTABLE}
    if offending:
        raise MixedProvenanceError(offending)

    records: list[CorpusRecord] = []
    for (
        attempt_id,
        run_id,
        obligation_id,
        provenance,
        model_id,
        weights,
        revision,
        ids_blob,
        logprobs_blob,
        verdict,
    ) in rows:
        exchanges: tuple[CorpusExchange, ...] = ()
        if ids_blob is not None:
            decoded = decode_trajectory_token_ids(await from_bytea(blobs, bytes(ids_blob)))
            logprobs = (
                decode_trajectory_logprobs(await from_bytea(blobs, bytes(logprobs_blob)))
                if logprobs_blob is not None
                else []
            )
            exchanges = tuple(
                CorpusExchange(
                    prompt_token_ids=exchange.prompt,
                    completion_token_ids=exchange.completions,
                    completion_logprobs=tuple(logprobs[i]) if i < len(logprobs) else (),
                    finish_reasons=exchange.finish_reasons,
                    sampling=exchange.sampling,
                    seed=exchange.seed,
                )
                for i, exchange in enumerate(decoded)
            )
        records.append(
            CorpusRecord(
                attempt_id=attempt_id,
                run_id=run_id,
                obligation_id=obligation_id,
                provenance=provenance,
                model_id=model_id,
                model_weights_hash=weights,
                tokenizer_revision=revision,
                verdict=verdict,
                exchanges=exchanges,
            )
        )
    return records


async def write_corpus(
    session_factory: async_sessionmaker[AsyncSession],
    blobs: BlobStore,
    run_ids: Sequence[uuid.UUID],
    path: Path,
) -> int:
    """Export `run_ids` to `path` as JSON Lines and return the record count.

    The file is opened only once every record is assembled, so a refused export leaves nothing at
    `path` -- not an empty file, which would read as "this run had no trajectories".
    """
    records = await export_corpus(session_factory, blobs, run_ids)
    path.write_text("".join(json.dumps(r.to_json(), sort_keys=True) + "\n" for r in records))
    return len(records)
