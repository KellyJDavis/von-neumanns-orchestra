"""M3.12 -- the corpus exporter raises on a mixed-provenance set (spec §7.1, Phase 3's exit).

Against a real PostgreSQL, with trajectories written by the real `TrajectoryWriter` as the real
`app` role -- the same path the executor takes -- so what is exported is what a run records. The
property under test is spec's sentence in full: it *raises*, it *lists the offending ids*, and it
*does not filter* -- which is also why nothing may be left at the output path when it refuses.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import SealedObligation
from lean_agent_core.blobs import LocalBlobStore
from lean_agent_core.corpus import EXPORTABLE, MixedProvenanceError, write_corpus
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.executor import TrajectoryWriter
from lean_agent_core.protocols import Completion, Exchange
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

#: Logprobs exactly representable in float32, so the round trip is checked for equality.
EXCHANGE = Exchange(
    prompt_token_ids=(151644, 872, 198),
    completions=(
        Completion(
            token_ids=(40, 1079, 151645),
            logprobs=(-0.5, -1.25, -0.125),
            text="I am",
            finish_reason="stop",
        ),
    ),
    sampling={"temperature": 0.8, "n": 1},
    seed=1234,
)


def _attempts(admin_engine: Engine, obligation: SealedObligation, count: int) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in range(count)]
    with admin_engine.connect() as conn:
        for attempt_id in ids:
            conn.execute(
                text(
                    "INSERT INTO attempt (id, obligation_id, run_id, policy_id, "
                    "policy_config_hash) VALUES (:id, :o, :r, 'corpus-test', :h)"
                ),
                {"id": attempt_id, "o": obligation.id, "r": obligation.run_id, "h": b"h"},
            )
        conn.commit()
    return ids


def _with_trajectories(
    url: str,
    blobs: LocalBlobStore,
    rows: list[tuple[uuid.UUID, ProvenanceClass]],
    then: Callable[[async_sessionmaker[AsyncSession]], Awaitable[Any]],
) -> Any:
    """Write one trajectory per row through the real writer, then run `then` -- one event loop,
    so the asyncpg engine is created and disposed inside it (M2.2)."""

    async def main() -> Any:
        engine = create_async_engine(url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            writer = TrajectoryWriter(sessions, blobs)
            for attempt_id, provenance in rows:
                symbolic = provenance is ProvenanceClass.SYMBOLIC
                await writer.write(
                    attempt_id=attempt_id,
                    provenance=provenance,
                    steps=[],
                    model_id=None if symbolic else "some/model",
                    exchanges=() if symbolic else (EXCHANGE,),
                    tokenizer_revision=None if symbolic else "abc123",
                )
            return await then(sessions)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def test_a_mixed_provenance_set_raises_naming_the_offenders_and_writes_nothing(
    admin_engine: Engine,
    sealed_obligation: SealedObligation,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    symbolic, open_weights, human, closed = _attempts(admin_engine, sealed_obligation, 4)
    blobs = LocalBlobStore(tmp_path / "blobs")
    out = tmp_path / "corpus.jsonl"

    async def export(sessions: async_sessionmaker[AsyncSession]) -> MixedProvenanceError:
        with pytest.raises(MixedProvenanceError) as raised:
            await write_corpus(sessions, blobs, [sealed_obligation.run_id], out)
        return raised.value

    error = _with_trajectories(
        app_async_database_url,
        blobs,
        [
            (symbolic, ProvenanceClass.SYMBOLIC),
            (open_weights, ProvenanceClass.OPEN_WEIGHTS),
            (human, ProvenanceClass.HUMAN),
            (closed, ProvenanceClass.CLOSED_API_EVAL_ONLY),
        ],
        export,
    )
    # Exactly the one that may not go -- not the whole run, and not "some".
    assert error.offending == {closed: "closed_api_eval_only"}
    assert str(closed) in str(error)
    # Not filtered: three exportable trajectories were right there, and none of them was written.
    assert not out.exists()


def test_a_clean_set_exports_every_trajectory_with_its_exact_token_ids(
    admin_engine: Engine,
    sealed_obligation: SealedObligation,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    symbolic, open_weights, human = _attempts(admin_engine, sealed_obligation, 3)
    blobs = LocalBlobStore(tmp_path / "blobs")
    out = tmp_path / "corpus.jsonl"

    async def export(sessions: async_sessionmaker[AsyncSession]) -> int:
        return await write_corpus(sessions, blobs, [sealed_obligation.run_id], out)

    count = _with_trajectories(
        app_async_database_url,
        blobs,
        [
            (symbolic, ProvenanceClass.SYMBOLIC),
            (open_weights, ProvenanceClass.OPEN_WEIGHTS),
            (human, ProvenanceClass.HUMAN),
        ],
        export,
    )
    records = {r["attempt_id"]: r for r in map(json.loads, out.read_text().splitlines())}
    assert count == 3
    assert set(records) == {str(symbolic), str(open_weights), str(human)}

    model = records[str(open_weights)]
    assert (model["provenance"], model["model_id"], model["tokenizer_revision"]) == (
        "open_weights",
        "some/model",
        "abc123",
    )
    (exchange,) = model["exchanges"]
    assert exchange["prompt_token_ids"] == [151644, 872, 198]
    assert exchange["completion_token_ids"] == [[40, 1079, 151645]]
    assert exchange["completion_logprobs"] == [[-0.5, -1.25, -0.125]]
    assert exchange["finish_reasons"] == ["stop"]
    assert exchange["seed"] == 1234
    # A symbolic trajectory is corpus too -- spec §7.1's "unencumbered training data at zero token
    # cost" -- with no model exchanges to carry.
    assert records[str(symbolic)]["exchanges"] == []


def test_the_exportable_set_is_spec_s_and_every_other_provenance_is_refused() -> None:
    """An allowlist over the enum: a provenance class added later is refused until someone decides
    otherwise here, rather than exported because nobody thought to exclude it."""
    assert {p.value for p in EXPORTABLE} == {"open_weights", "symbolic", "human"}
    assert set(ProvenanceClass) - EXPORTABLE == {ProvenanceClass.CLOSED_API_EVAL_ONLY}
