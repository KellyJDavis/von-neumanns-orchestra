"""M3.6 -- `model_response_cache` against a real PostgreSQL, as the real `app` role.

Lives under `tests/db/` for the reason `test_cache.py` and `test_verdicts.py` do: it needs a live
database with migrations *and* `deploy/grants.sql` applied, and no Lean process at all.

The interesting assertions are not "a value round-trips". They are that unseeded sampling is
refused, that the float32 storage spec asked for is what actually happens, and that the key
separates the things it must.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from lean_agent_core.blobs import INLINE_THRESHOLD, LocalBlobStore
from lean_agent_core.codecs import LOGPROB_BYTES, TOKEN_BYTES, pack_logprobs, unpack_logprobs
from lean_agent_core.protocols import Completion, CompletionResponse, SamplingParams
from lean_agent_models.cache import (
    ResponseCacheStore,
    compute_response_cache_key,
    is_cacheable,
)
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PROMPT = (9707, 11, 1879)


def _response(
    *,
    model_id: str = "Qwen/Qwen3-0.6B",
    completions: tuple[Completion, ...] | None = None,
    elapsed_ms: int = 42,
) -> CompletionResponse:
    return CompletionResponse(
        completions=completions
        or (
            Completion(
                token_ids=(13, 358, 2776),
                logprobs=(-1.4426193237304688, -1.9816466569900513, -0.8837368488311768),
                text=". I'm",
                finish_reason="length",
            ),
        ),
        prompt_token_ids=PROMPT,
        model_id=model_id,
        model_weights_hash="w-abc",
        tokenizer_revision="t-def",
        elapsed_ms=elapsed_ms,
    )


@pytest.fixture
def store(app_async_database_url: str, tmp_path: Path) -> Iterator[ResponseCacheStore]:
    """A store on the real `app` role, with its own blob directory."""
    engine = create_async_engine(app_async_database_url)
    yield ResponseCacheStore(
        async_sessionmaker(engine, expire_on_commit=False), LocalBlobStore(tmp_path / "blobs")
    )
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _clean(admin_engine: Engine) -> Iterator[None]:
    """Rows here are global -- the cache is deliberately not tenant-scoped -- so they are cleaned
    up explicitly rather than by a rolled-back transaction, the same reason `test_privileges.py`
    cannot use that isolation pattern.

    As **admin**, not as `app`: `app` holds INSERT and UPDATE on this table and deliberately no
    DELETE, since eviction is a maintenance job rather than something a worker does mid-run. A
    test cleaning up through the role under test would have needed that grant to exist.
    """
    yield
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM model_response_cache"))
        conn.commit()


# --------------------------------------------------------------------------------------------
# What may be cached at all.
# --------------------------------------------------------------------------------------------


def test_unseeded_sampling_is_not_cacheable() -> None:
    """The correctness rule this module exists to enforce.

    Two unseeded requests at a nonzero temperature share a key and are asking for *different*
    answers. Serving the second from cache would turn `WholeProofSampler`'s "sample n proofs and
    check them all" into one proof checked n times -- and the pass rate would move for a reason
    nothing in the trajectory would show.
    """
    sampled = SamplingParams(temperature=0.8, n=8)
    assert is_cacheable(sampled, seed=None) is False
    assert is_cacheable(sampled, seed=1234) is True


def test_greedy_decoding_is_cacheable_without_a_seed() -> None:
    """`temperature == 0` is already a request for a reproducible answer, so returning the one the
    server already gave is what the caller asked for."""
    assert is_cacheable(SamplingParams(temperature=0.0), seed=None) is True


# --------------------------------------------------------------------------------------------
# The key.
# --------------------------------------------------------------------------------------------


def test_the_key_separates_everything_spec_names() -> None:
    """`sha256(prompt_tokens ‖ model_id ‖ canonical(sampling) ‖ seed)` -- each component changed
    alone must move the key, or two different questions would share an answer."""
    base = compute_response_cache_key(PROMPT, "m", SamplingParams(), 1)
    assert base == compute_response_cache_key(PROMPT, "m", SamplingParams(), 1)

    variants = {
        "prompt": compute_response_cache_key((9707, 11, 1880), "m", SamplingParams(), 1),
        "model": compute_response_cache_key(PROMPT, "other", SamplingParams(), 1),
        "temperature": compute_response_cache_key(PROMPT, "m", SamplingParams(temperature=0.8), 1),
        "n": compute_response_cache_key(PROMPT, "m", SamplingParams(n=4), 1),
        "stop": compute_response_cache_key(PROMPT, "m", SamplingParams(stop=("x",)), 1),
        "seed": compute_response_cache_key(PROMPT, "m", SamplingParams(), 2),
        "no_seed": compute_response_cache_key(PROMPT, "m", SamplingParams(), None),
    }
    for name, key in variants.items():
        assert key != base, name
    assert len(set(variants.values())) == len(variants), "no two variants may collide"


def test_a_prompt_that_differs_only_in_order_is_a_different_key() -> None:
    """Hashing the packed bytes rather than a rendering means the ids themselves decide the key."""
    assert compute_response_cache_key((1, 2), "m", SamplingParams(), 1) != (
        compute_response_cache_key((2, 1), "m", SamplingParams(), 1)
    )


# --------------------------------------------------------------------------------------------
# Storage, against the real table.
# --------------------------------------------------------------------------------------------


def test_a_response_round_trips_through_postgres(store: ResponseCacheStore) -> None:
    key = compute_response_cache_key(PROMPT, "Qwen/Qwen3-0.6B", SamplingParams(), 1)
    original = _response()

    async def main() -> None:
        assert await store.get(key) is None
        await store.put(key, original)
        cached = await store.get(key)

        assert cached is not None
        assert cached.model_id == original.model_id
        assert cached.prompt_token_ids == PROMPT
        assert cached.elapsed_ms == original.elapsed_ms
        # Recorded so a cache hit can still say exactly what produced it (§7.3) -- a hit that
        # could not would make the trajectory it fills unreplayable.
        assert cached.weights_revision == "w-abc"
        assert cached.tokenizer_revision == "t-def"

        (completion,) = cached.completions
        assert completion.token_ids == original.completions[0].token_ids
        assert completion.text == original.completions[0].text
        assert completion.finish_reason == "length"

    asyncio.run(main())


def test_logprobs_are_stored_as_float32_and_real_ones_survive_it(
    store: ResponseCacheStore,
) -> None:
    """Spec §6.5 sizes the corpus at "about 4 GB per 10⁹ tokens", which is 4 bytes each -- so the
    storage is float32 and this pins it against a future change to JSON, which would cost three to
    five times that and quietly make the budget wrong.

    The happy surprise, found by writing this test wrongly first: **real logprobs lose nothing.**
    The values below are genuine vLLM output, and vLLM computes them in float32, so the round trip
    is exact. The first version of this test asserted that storage was lossy and failed, which is
    the better fact to record. A value that genuinely needs float64 is used separately below to
    show the format really is float32 rather than accidentally wider.
    """
    key = compute_response_cache_key(PROMPT, "m", SamplingParams(), 7)
    original = _response()
    real_logprobs = original.completions[0].logprobs

    assert unpack_logprobs(pack_logprobs(real_logprobs)) == real_logprobs
    assert len(pack_logprobs(real_logprobs)) == len(real_logprobs) * LOGPROB_BYTES == 12

    async def main() -> None:
        await store.put(key, original)
        cached = await store.get(key)
        assert cached is not None
        assert cached.completions[0].logprobs == real_logprobs

    asyncio.run(main())


def test_the_codec_really_is_float32_and_not_something_wider() -> None:
    """The other half: a value needing float64 is narrowed, so the previous test passing is
    evidence about vLLM's output rather than about the codec being a no-op."""
    needs_float64 = (0.1234567890123456789, -2.718281828459045)
    narrowed = unpack_logprobs(pack_logprobs(needs_float64))

    assert narrowed != needs_float64
    for stored, live in zip(narrowed, needs_float64, strict=True):
        assert abs(stored - live) < 1e-7
    # Deterministic, which is what a cache key over these bytes depends on.
    assert pack_logprobs(needs_float64) == pack_logprobs(needs_float64)


def test_every_sample_of_one_request_is_one_row(
    store: ResponseCacheStore, admin_engine: Engine
) -> None:
    """`n=8` is one cached request, not eight cached completions: serving three of eight from cache
    and re-sampling the rest would be a different distribution than the one asked for."""
    completions = tuple(
        Completion(
            token_ids=(100 + i, 200 + i),
            logprobs=(-0.5 - i, -1.5 - i),
            text=f"sample {i}",
            finish_reason="length",
        )
        for i in range(4)
    )
    key = compute_response_cache_key(PROMPT, "m", SamplingParams(n=4, temperature=0.8), 11)

    async def main() -> None:
        await store.put(key, _response(completions=completions))
        cached = await store.get(key)
        assert cached is not None
        assert len(cached.completions) == 4
        assert [c.text for c in cached.completions] == [f"sample {i}" for i in range(4)]
        assert [c.token_ids for c in cached.completions] == [c.token_ids for c in completions]

    asyncio.run(main())

    with admin_engine.connect() as conn:
        rows, n = conn.execute(
            text("SELECT count(*), max(n_completions) FROM model_response_cache")
        ).one()
    assert (rows, n) == (1, 4)


def test_a_large_response_goes_to_the_blob_store_not_the_column(
    store: ResponseCacheStore, tmp_path: Path
) -> None:
    """`store_or_inline`'s §5.3 threshold applies here like everywhere else -- a long completion
    must not be pushed into a `bytea` column."""
    long_completion = Completion(
        token_ids=tuple(range(30_000)),
        logprobs=tuple(-0.001 * i for i in range(30_000)),
        text="x" * 1000,
        finish_reason="length",
    )
    assert len(long_completion.token_ids) * TOKEN_BYTES > INLINE_THRESHOLD
    assert len(long_completion.logprobs) * LOGPROB_BYTES > INLINE_THRESHOLD
    key = compute_response_cache_key(PROMPT, "m", SamplingParams(), 13)

    async def main() -> None:
        await store.put(key, _response(completions=(long_completion,)))
        cached = await store.get(key)
        assert cached is not None
        assert cached.completions[0].token_ids == long_completion.token_ids
        assert len(cached.completions[0].logprobs) == 30_000

    asyncio.run(main())
    assert any((tmp_path / "blobs").rglob("*")), "the large payload should be in the CAS"


def test_a_second_put_for_one_key_is_absorbed_rather_than_raising(
    store: ResponseCacheStore,
) -> None:
    """Two workers computing the same content-addressed key concurrently is the expected case, not
    a race to detect -- the same reason `VerificationCacheStore.put` uses `ON CONFLICT DO NOTHING`.
    (`VerdictWriter.write` deliberately does the opposite, because one attempt gets one verdict.)"""
    key = compute_response_cache_key(PROMPT, "m", SamplingParams(), 17)

    async def main() -> None:
        await store.put(key, _response(elapsed_ms=10))
        await store.put(key, _response(elapsed_ms=999))
        cached = await store.get(key)
        assert cached is not None
        assert cached.elapsed_ms == 10, "the first answer stored wins"

    asyncio.run(main())


def test_a_hit_is_counted(store: ResponseCacheStore, app_database_url: str) -> None:
    """`hits`/`last_hit_at` are what a future eviction policy would rank on, so a hit that did not
    record itself would leave that policy blind."""
    key = compute_response_cache_key(PROMPT, "m", SamplingParams(), 19)

    async def main() -> None:
        await store.put(key, _response())
        for _ in range(3):
            assert await store.get(key) is not None

    asyncio.run(main())

    engine = create_engine(app_database_url)
    with engine.connect() as conn:
        hits, last_hit = conn.execute(
            text("SELECT hits, last_hit_at FROM model_response_cache WHERE cache_key = :k"),
            {"k": key},
        ).one()
    engine.dispose()
    assert hits == 3
    assert last_hit is not None


def test_a_miss_returns_none_rather_than_raising(store: ResponseCacheStore) -> None:
    async def main() -> None:
        assert await store.get(bytes(32)) is None

    asyncio.run(main())


def test_the_app_role_may_write_this_cache_but_not_delete_from_it(app_database_url: str) -> None:
    """Unlike `verification_cache`, which only `leanserv` may write: the two sit on opposite sides
    of that boundary, one recording what the *kernel* said and this one the agent's own work.

    DELETE is withheld on purpose. Eviction is a maintenance job with a policy behind it, not
    something a worker does mid-run, and spec §5.5's whole approach is to grant the narrowest thing
    that works rather than whatever is convenient.
    """
    engine = create_engine(app_database_url)
    key = uuid.uuid4().bytes
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO model_response_cache (cache_key, model_id, completions_blob, "
                    "n_completions, elapsed_ms) VALUES (:k, 'm', :c, 1, 1)"
                ),
                {"k": key, "c": b"\x00[]"},
            )
            conn.execute(
                text("UPDATE model_response_cache SET hits = hits + 1 WHERE cache_key = :k"),
                {"k": key},
            )
            conn.commit()

        with engine.connect() as conn, pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("DELETE FROM model_response_cache WHERE cache_key = :k"), {"k": key})
    finally:
        engine.dispose()
