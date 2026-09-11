"""M3.6 -- packing token ids and logprobs (spec §6.5's storage format).

No infrastructure. What is under test is a format two things will depend on: the response cache
(M3.6) and `trajectory.token_ids_blob`/`logprobs_blob` (M3.7). If those two ever disagreed, a
trajectory replayed from the cache would not match the one it replayed.
"""

from __future__ import annotations

import struct

import pytest
from lean_agent_core.codecs import (
    LOGPROB_BYTES,
    TOKEN_BYTES,
    CodecError,
    pack_logprobs,
    pack_token_ids,
    round_trip_logprobs,
    unpack_logprobs,
    unpack_token_ids,
)


def test_token_ids_round_trip_exactly() -> None:
    ids = (0, 1, 151_643, 32_000, 9707)
    assert unpack_token_ids(pack_token_ids(ids)) == ids
    assert len(pack_token_ids(ids)) == len(ids) * TOKEN_BYTES


def test_the_widest_real_vocabulary_fits_with_room() -> None:
    """Qwen3's vocabulary reaches past 151,000; int32 covers that roughly fourteen thousand times
    over, so the format is not going to need revisiting for a larger tokenizer."""
    assert unpack_token_ids(pack_token_ids((2**31 - 1,))) == (2**31 - 1,)


def test_an_out_of_range_token_id_raises_rather_than_wrapping() -> None:
    """Signed rather than unsigned deliberately: under `<I` a caller passing `-1` as a sentinel
    would silently get token 4294967295 back, which is a plausible-looking id."""
    with pytest.raises(CodecError, match="int32"):
        pack_token_ids((2**31,))


def test_logprobs_are_four_bytes_each() -> None:
    """Spec's "about 4 GB per 10⁹ tokens" is this arithmetic and no other."""
    assert LOGPROB_BYTES == 4
    logprobs = (-1.5, -0.25, -10.0)
    assert len(pack_logprobs(logprobs)) == len(logprobs) * LOGPROB_BYTES


def test_a_value_that_fits_float32_survives_and_one_that_does_not_is_narrowed() -> None:
    exact = (-1.5, -0.25, -2.0)
    assert unpack_logprobs(pack_logprobs(exact)) == exact

    inexact = (0.1234567890123456789,)
    assert round_trip_logprobs(inexact) != inexact
    assert abs(round_trip_logprobs(inexact)[0] - inexact[0]) < 1e-7


def test_packing_is_deterministic() -> None:
    """A cache key is taken over these bytes, so the same value must pack the same way every time
    or two identical requests would miss each other."""
    logprobs = (-1.4426193237304688, -1.9816466569900513)
    assert pack_logprobs(logprobs) == pack_logprobs(logprobs)
    assert pack_token_ids((13, 358)) == pack_token_ids((13, 358))


def test_the_byte_order_is_pinned_not_native() -> None:
    """These bytes go into a database another machine will read. Native order would make a corpus
    unreadable on a big-endian host, and native alignment would insert padding the length
    arithmetic does not expect."""
    assert pack_token_ids((1,)) == struct.pack("<i", 1)
    assert pack_logprobs((1.0,)) == struct.pack("<f", 1.0)
    assert len(pack_token_ids((1, 2, 3))) == 3 * TOKEN_BYTES


def test_truncated_bytes_raise_rather_than_decoding_short() -> None:
    """A partially-decoded logprob array would be silently shorter than its token ids, and the two
    being parallel is the entire reason to store them together."""
    with pytest.raises(CodecError, match="not a whole number of float32"):
        unpack_logprobs(pack_logprobs((-1.0, -2.0))[:-1])
    with pytest.raises(CodecError, match="not a whole number of int32"):
        unpack_token_ids(pack_token_ids((1, 2))[:-1])


def test_empty_sequences_are_legal() -> None:
    """A completion that produced no tokens is a real outcome, not an error to encode around."""
    assert pack_token_ids(()) == b""
    assert unpack_token_ids(b"") == ()
    assert unpack_logprobs(pack_logprobs(())) == ()


def test_each_completions_finish_reason_survives_the_round_trip() -> None:
    """M3.11. `length` means the budget cut a sample off, and the viewer has to be able to say that
    about one sample -- the aggregate in the step's detail cannot say *which*."""
    from lean_agent_core.codecs import decode_trajectory_token_ids, encode_trajectory_token_ids
    from lean_agent_core.protocols import Completion, Exchange

    samples = (
        Completion(token_ids=(1, 2), logprobs=(-0.1, -0.2), text="a", finish_reason="stop"),
        Completion(token_ids=(3,), logprobs=(-0.3,), text="b", finish_reason="length"),
    )
    (exchange,) = decode_trajectory_token_ids(
        encode_trajectory_token_ids([Exchange((7, 8), samples, {"n": 2}, 11)])
    )
    assert exchange.finish_reasons == ("stop", "length")


def test_an_exchange_recorded_before_finish_reasons_reads_as_not_recorded() -> None:
    """Honest absence: an empty tuple, never a guessed `stop`."""
    import json

    from lean_agent_core.codecs import decode_trajectory_token_ids, pack_token_ids

    legacy = json.dumps(
        {
            "exchanges": [
                {
                    "prompt": pack_token_ids((1,)).hex(),
                    "completions": [],
                    "sampling": {},
                    "seed": None,
                }
            ]
        }
    ).encode()
    (exchange,) = decode_trajectory_token_ids(legacy)
    assert exchange.finish_reasons == ()
