"""M3.2 -- the recorded fixtures and the server that replays them.

Two things are under test here, and the second matters more than the first. The server is small
and its behaviour is easy to check. The *fixtures* are the thing everything downstream will trust:
if they drift from what a real vLLM emits, every model-layer test after this one passes against a
fiction. So most of what follows holds the fixture file to what it claims -- that the recorded
interactions really were recorded, that they carry the fields §6.5 depends on, and that the two
things this project asserted about vLLM (token ids in, logprobs out; determinism at a fixed seed)
are visible in bytes a real server produced rather than in a claim.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from replay_server import Fixtures, create_replay_app, load_fixtures


@pytest.fixture(scope="module")
def fixtures() -> Fixtures:
    return load_fixtures()


def _post(app: Any, body: dict[str, Any]) -> httpx.Response:
    async def main() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://replay"
        ) as client:
            return await client.post("/v1/completions", json=body)

    return asyncio.run(main())


# --------------------------------------------------------------------------------------------
# The fixtures themselves.
# --------------------------------------------------------------------------------------------


def test_the_fixtures_say_where_they_came_from(fixtures: Fixtures) -> None:
    """Provenance is the whole basis for trusting these bytes. Without it, a later reader cannot
    tell a recording from an invention, which is exactly the distinction this milestone exists to
    preserve."""
    assert "vllm" in fixtures.provenance["recorded_from"]
    assert fixtures.provenance["model_id"] == "Qwen/Qwen3-0.6B"
    assert fixtures.provenance["endpoint_path"] == "/v1/completions"
    assert fixtures.provenance["recorded_at"]


def test_constructed_interactions_are_marked_and_say_why(fixtures: Fixtures) -> None:
    """Two fixtures model servers that misbehave, which no correctly-behaving server can produce.
    They are legitimate, and they must never be mistaken for evidence about vLLM."""
    constructed = {i.name for i in fixtures.interactions if not i.recorded}
    assert constructed == {"missing_logprobs", "logprobs_dropped", "ragged_logprobs"}
    for name in constructed:
        assert "Constructed" in fixtures.by_name(name).note

    recorded = [i for i in fixtures.interactions if i.recorded]
    assert len(recorded) >= 5
    assert all("Constructed" not in i.note for i in recorded)


def test_every_recorded_success_carries_what_spec_6_5_requires(fixtures: Fixtures) -> None:
    """Token ids out and a logprob per sampled token, parallel. If a re-record ever lost either,
    the whole model layer would be building on a response shape that cannot satisfy §6.5, and
    every later test would be the wrong kind of green."""
    successes = [i for i in fixtures.interactions if i.recorded and i.status == 200]
    assert successes

    for interaction in successes:
        choices = interaction.response["choices"]
        assert choices, interaction.name
        for choice in choices:
            token_ids = choice["token_ids"]
            logprobs = choice["logprobs"]["token_logprobs"]
            tokens = choice["logprobs"]["tokens"]
            assert token_ids, interaction.name
            assert len(logprobs) == len(token_ids) == len(tokens), interaction.name
            assert all(isinstance(lp, float) for lp in logprobs), interaction.name
            assert choice["finish_reason"] in {"length", "stop"}, interaction.name


def test_a_token_id_prompt_was_accepted_and_echoed_back(fixtures: Fixtures) -> None:
    """§6.5's load-bearing claim, as recorded bytes: the prompt went out as integers and the
    server returned the same integers, so nothing re-tokenized in between."""
    interaction = fixtures.by_name("greedy_single")
    sent = interaction.request["prompt"]
    assert sent == [9707, 11, 1879]
    assert all(isinstance(token, int) for token in sent)
    assert interaction.response["choices"][0]["prompt_token_ids"] == sent


def test_the_real_server_was_deterministic_at_a_fixed_seed(fixtures: Fixtures) -> None:
    """Recorded rather than asserted. The pair was captured as two separate requests with
    identical bodies; that their responses agree on tokens *and* logprobs is what makes spec
    §7.2's R1 "token-identical" tier observable on fixed hardware."""
    first = fixtures.by_name("greedy_single")
    second = fixtures.by_name("greedy_single_repeat")
    assert first.request == second.request
    assert first.recorded and second.recorded

    a, b = first.response["choices"][0], second.response["choices"][0]
    assert a["token_ids"] == b["token_ids"]
    assert a["logprobs"]["token_logprobs"] == b["logprobs"]["token_logprobs"]
    assert a["text"] == b["text"]


def test_the_sampled_fixture_really_has_several_distinct_completions(fixtures: Fixtures) -> None:
    """`n>1` is how `WholeProofSampler` will run, and a fixture whose four completions were
    identical would not exercise anything about handling several."""
    choices = fixtures.by_name("sampled_n4").response["choices"]
    assert len(choices) == 4
    assert len({tuple(c["token_ids"]) for c in choices}) > 1
    assert [c["index"] for c in choices] == [0, 1, 2, 3]


def test_both_terminal_finish_reasons_are_covered(fixtures: Fixtures) -> None:
    assert fixtures.by_name("stop_string").response["choices"][0]["finish_reason"] == "stop"
    assert fixtures.by_name("long_max_tokens").response["choices"][0]["finish_reason"] == "length"


def test_a_real_error_response_was_recorded_not_imagined(fixtures: Fixtures) -> None:
    """The client's error path deserves real bytes too -- an invented error body is exactly the
    kind of thing that parses fine in a test and not in production."""
    interaction = fixtures.by_name("unknown_model")
    assert interaction.recorded is True
    assert interaction.status == 404
    assert interaction.response


# --------------------------------------------------------------------------------------------
# The server.
# --------------------------------------------------------------------------------------------


def test_a_matching_request_gets_the_recorded_response_verbatim(fixtures: Fixtures) -> None:
    app = create_replay_app(fixtures)
    interaction = fixtures.by_name("greedy_single")
    response = _post(app, interaction.request)
    assert response.status_code == 200
    assert response.json() == interaction.response


def test_a_recorded_error_replays_with_its_real_status(fixtures: Fixtures) -> None:
    app = create_replay_app(fixtures)
    interaction = fixtures.by_name("unknown_model")
    response = _post(app, interaction.request)
    assert response.status_code == 404
    assert response.json() == interaction.response


def test_an_unmatched_request_is_a_loud_409_not_a_plausible_default(fixtures: Fixtures) -> None:
    """The failure this design exists to prevent: if the client quietly changed what it sends --
    dropped `return_token_ids`, renamed a sampling field -- a server that answered anyway would
    keep every test green against an answer to a question nobody asked."""
    app = create_replay_app(fixtures)
    request = dict(fixtures.by_name("greedy_single").request)
    del request["return_token_ids"]

    response = _post(app, request)
    assert response.status_code == 409
    body = response.json()
    assert "no recorded interaction matches" in body["error"]
    # It says what it *does* have, so the diagnosis is in the failure rather than a debugging trip.
    assert {entry["name"] for entry in body["available"]} == {i.name for i in fixtures.interactions}
    assert "re-record" in body["hint"]


def test_a_changed_sampling_value_does_not_match(fixtures: Fixtures) -> None:
    """Matching is on the whole body, so a different temperature is a different question. A
    fixture set that answered it anyway would make the sampling parameters untested."""
    app = create_replay_app(fixtures)
    request = dict(fixtures.by_name("greedy_single").request) | {"temperature": 0.9}
    assert _post(app, request).status_code == 409


def test_the_models_endpoint_answers_a_readiness_check(fixtures: Fixtures) -> None:
    async def main() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_replay_app(fixtures)),
            base_url="http://replay",
        ) as client:
            return await client.get("/v1/models")

    response = asyncio.run(main())
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "Qwen/Qwen3-0.6B"


def test_by_name_raises_on_a_fixture_that_does_not_exist(fixtures: Fixtures) -> None:
    """A test asking for a fixture that was renamed should fail, not silently receive `None` and
    assert nothing."""
    with pytest.raises(KeyError, match="no recorded interaction named"):
        fixtures.by_name("never_recorded")
