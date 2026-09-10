"""M3.7 -- the four `trajectory` columns Phase 2 left NULL, against a real PostgreSQL.

`token_ids_blob`, `logprobs_blob`, `model_weights_hash` and `tokenizer_revision` have existed since
M1.5 and were never written, which was right while Phase 2 made zero model calls. This is where
they start being filled, and §9 is the reason it matters: token ids and logprobs are listed among
the things that "cannot be recomputed correctly later", so an attempt whose completions were not
recorded is one that can never be trained on or replayed.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import SealedObligation
from lean_agent_core.blobs import LocalBlobStore, from_bytea
from lean_agent_core.codecs import decode_trajectory_logprobs, decode_trajectory_token_ids
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.executor import TrajectoryWriter
from lean_agent_core.protocols import Completion
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PROMPT = (9707, 11, 1879)
SAMPLES = (
    Completion(
        token_ids=(13, 358, 2776),
        logprobs=(-1.4426193237304688, -1.9816466569900513, -0.8837368488311768),
        text=". I'm",
        finish_reason="length",
    ),
    Completion(
        token_ids=(264, 5458),
        logprobs=(-1.8618056774139404, -2.737832546234131),
        text=" a student",
        finish_reason="stop",
    ),
)


@pytest.fixture
def attempt(admin_engine: Engine, sealed_obligation: SealedObligation) -> Iterator[uuid.UUID]:
    """One real `attempt` row to hang a trajectory off -- `trajectory.attempt_id` is a foreign key,
    so there is no writing one without it."""
    attempt_id = uuid.uuid4()
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO attempt (id, obligation_id, run_id, policy_id, policy_config_hash) "
                "VALUES (:id, :o, :r, 'p', 'c')"
            ),
            {"id": attempt_id, "o": sealed_obligation.id, "r": sealed_obligation.run_id},
        )
        conn.commit()
    yield attempt_id
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM attempt WHERE id = :id"), {"id": attempt_id})
        conn.commit()


def _writer(url: str, tmp_path: Path) -> tuple[TrajectoryWriter, LocalBlobStore]:
    engine = create_async_engine(url)
    blobs = LocalBlobStore(tmp_path / "blobs")
    return TrajectoryWriter(async_sessionmaker(engine, expire_on_commit=False), blobs), blobs


def test_completions_round_trip_through_the_trajectory_columns(
    app_async_database_url: str, admin_engine: Engine, attempt: uuid.UUID, tmp_path: Path
) -> None:
    engine = create_async_engine(app_async_database_url)
    blobs = LocalBlobStore(tmp_path / "blobs")
    writer = TrajectoryWriter(async_sessionmaker(engine, expire_on_commit=False), blobs)

    async def main() -> None:
        try:
            await writer.write(
                attempt_id=attempt,
                provenance=ProvenanceClass.OPEN_WEIGHTS,
                steps=[],
                sampling={"temperature": 0.8, "n": 2},
                model_id="Goedel-LM/Goedel-Prover-V2-8B",
                seed=1234,
                prompt_token_ids=PROMPT,
                completions=SAMPLES,
                model_weights_hash="w-abc",
                tokenizer_revision="t-def",
            )
        finally:
            await engine.dispose()

    asyncio.run(main())

    with admin_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT provenance::text, model_id, model_weights_hash, tokenizer_revision, "
                "sampling, seed, token_ids_blob, logprobs_blob FROM trajectory "
                "WHERE attempt_id = :id"
            ),
            {"id": attempt},
        ).one()

    provenance, model_id, weights, tokenizer, sampling, seed, tokens_blob, logprobs_blob = row
    assert provenance == "open_weights"
    assert (model_id, weights, tokenizer) == (
        "Goedel-LM/Goedel-Prover-V2-8B",
        "w-abc",
        "t-def",
    )
    assert sampling == {"temperature": 0.8, "n": 2}
    assert seed == 1234
    assert tokens_blob is not None
    assert logprobs_blob is not None

    async def read() -> None:
        prompt, completions = decode_trajectory_token_ids(
            await from_bytea(blobs, bytes(tokens_blob))
        )
        logprobs = decode_trajectory_logprobs(await from_bytea(blobs, bytes(logprobs_blob)))

        # The prompt/completion boundary survives, which is the only thing replay needs from this
        # column: on-policy RL cannot compute a loss over tokens it cannot separate from the
        # context they were conditioned on.
        assert prompt == PROMPT
        assert completions == [s.token_ids for s in SAMPLES]
        assert logprobs == [s.logprobs for s in SAMPLES]
        for ids, lps in zip(completions, logprobs, strict=True):
            assert len(ids) == len(lps)

    asyncio.run(read())


def test_a_symbolic_attempt_leaves_the_token_columns_null(
    app_async_database_url: str, admin_engine: Engine, attempt: uuid.UUID, tmp_path: Path
) -> None:
    """The Phase 2 shape, unchanged. A symbolic attempt has no tokens, and writing empty blobs
    instead of NULL would make "was a model involved" unanswerable from the row."""
    engine = create_async_engine(app_async_database_url)
    writer = TrajectoryWriter(
        async_sessionmaker(engine, expire_on_commit=False), LocalBlobStore(tmp_path / "blobs")
    )

    async def main() -> None:
        try:
            await writer.write(attempt_id=attempt, provenance=ProvenanceClass.SYMBOLIC, steps=[])
        finally:
            await engine.dispose()

    asyncio.run(main())

    with admin_engine.connect() as conn:
        tokens, logprobs, model_id, weights = conn.execute(
            text(
                "SELECT token_ids_blob, logprobs_blob, model_id, model_weights_hash "
                "FROM trajectory WHERE attempt_id = :id"
            ),
            {"id": attempt},
        ).one()
    assert (tokens, logprobs, model_id, weights) == (None, None, None, None)


def test_a_long_completion_goes_to_the_blob_store(
    app_async_database_url: str, admin_engine: Engine, attempt: uuid.UUID, tmp_path: Path
) -> None:
    """`store_or_inline`'s §5.3 threshold applies here as everywhere: a long sample must not be
    pushed into a `bytea` column."""
    long_sample = Completion(
        token_ids=tuple(range(30_000)),
        logprobs=tuple(-0.001 * i for i in range(30_000)),
        text="x",
        finish_reason="length",
    )
    engine = create_async_engine(app_async_database_url)
    blobs = LocalBlobStore(tmp_path / "blobs")
    writer = TrajectoryWriter(async_sessionmaker(engine, expire_on_commit=False), blobs)

    async def main() -> None:
        try:
            await writer.write(
                attempt_id=attempt,
                provenance=ProvenanceClass.OPEN_WEIGHTS,
                steps=[],
                model_id="m",
                prompt_token_ids=PROMPT,
                completions=(long_sample,),
            )
        finally:
            await engine.dispose()

    asyncio.run(main())
    assert any((tmp_path / "blobs").rglob("*")), "the large payload should be in the CAS"

    with admin_engine.connect() as conn:
        tokens_blob = conn.execute(
            text("SELECT token_ids_blob FROM trajectory WHERE attempt_id = :id"), {"id": attempt}
        ).scalar_one()

    async def read() -> None:
        _, completions = decode_trajectory_token_ids(await from_bytea(blobs, bytes(tokens_blob)))
        assert completions[0] == long_sample.token_ids

    asyncio.run(read())
