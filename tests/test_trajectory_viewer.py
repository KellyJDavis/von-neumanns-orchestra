"""M3.11 -- the trajectory viewer's page, on its own.

No infrastructure: the page is a pure function of a `TrajectoryResponse`. The claim that its data
is exactly what was sent -- prompts decoded from the stored ids, samples as the server returned
them -- is held end to end against a recorded Goedel run in `tests/leanserv/test_whole_proof.py`.
What is pinned here is how the page treats that data: model text is untrusted and never markup,
and nothing is dropped on the way to the screen.
"""

from __future__ import annotations

import uuid

from lean_agent_api.schemas import (
    CompletionBody,
    ExchangeBody,
    PromptBody,
    StepBody,
    TrajectoryResponse,
    VerdictDetail,
)
from lean_agent_api.viewer import render_html

HOSTILE = "<script>alert('x')</script>"


def trajectory(**overrides: object) -> TrajectoryResponse:
    exchange = ExchangeBody(
        index=0,
        sampling={"n": 1},
        seed=11,
        prompt=PromptBody(
            token_count=3, text=f"<|im_start|>user\n{HOSTILE}", decoded_with="abc123"
        ),
        completions=[
            CompletionBody(
                index=0, token_count=2, text=HOSTILE, finish_reason="stop", logprob_sum=-1.5
            )
        ],
    )
    base: dict[str, object] = {
        "attempt_id": uuid.uuid4(),
        "provenance": "open_weights",
        "model_id": "Goedel-LM/Goedel-Prover-V2-8B",
        "tokenizer_revision": "abc123",
        "n_steps": 2,
        "steps": [
            StepBody(label="prover", action="RequestCompletion", ok=True, exchange=0),
            StepBody(
                label="sample 0",
                action="SubmitProof/check",
                ok=False,
                detail=HOSTILE,
                development=f"theorem G_1 : True := {HOSTILE}",
                diagnostics=[f"<input>:1:0-1:5: error: {HOSTILE}"],
            ),
        ],
        "exchanges": [exchange],
    }
    base.update(overrides)
    return TrajectoryResponse(**base)  # type: ignore[arg-type]


def test_model_text_is_never_markup() -> None:
    """Spec §7.2 treats AI-generated proofs as malicious code, and a prompt is model input: every
    place either can reach -- prompt, sample, step detail, development, diagnostic -- is escaped."""
    page = render_html(trajectory())
    assert "<script>" not in page
    assert page.count("&lt;script&gt;") == 5


def test_special_tokens_are_shown_not_swallowed() -> None:
    """`<|im_start|>` is part of what the model saw; unescaped, a browser would drop it as an
    unknown tag and the page would show a prompt nobody sent."""
    page = render_html(trajectory())
    assert "&lt;|im_start|&gt;user" in page


def test_a_request_is_shown_with_its_own_exchange_and_a_submission_with_its_development() -> None:
    page = render_html(trajectory())
    prompt_at = page.index(
        "Prompt &mdash; 3 tokens, decoded from its token ids with tokenizer abc123"
    )
    submitted_at = page.index("Submitted development")
    assert prompt_at < submitted_at, "steps are shown in the order they happened"
    assert "finish stop" in page and "log-probability -1.50" in page
    assert "Diagnostics (1)" in page


def test_an_undecodable_prompt_shows_its_ids_and_says_why() -> None:
    """Never a reconstruction: without the recorded tokenizer the page shows the ids themselves."""
    undecoded = ExchangeBody(
        index=0,
        sampling={},
        seed=None,
        prompt=PromptBody(
            token_count=3,
            text=None,
            decoded_with=None,
            token_ids=[9707, 11, 1879],
            note="tokenizer abc123 is not available to this deployment; showing token ids",
        ),
        completions=[],
    )
    page = render_html(trajectory(exchanges=[undecoded]))
    assert "token ids only" in page
    assert "9707 11 1879" in page
    assert "is not available to this deployment" in page


def test_an_exchange_no_step_points_at_is_still_shown() -> None:
    """Trajectories written before steps carried their exchange index have none; their exchanges
    are listed after the steps rather than silently left out."""
    steps = [StepBody(label="prover", action="RequestCompletion", ok=True)]
    page = render_html(trajectory(steps=steps, n_steps=1))
    assert "<h3>Exchange 0</h3>" in page


def test_the_verdict_or_its_absence_is_stated() -> None:
    assert "no verdict: nothing reached" in render_html(trajectory())
    proved = VerdictDetail(
        kind="proved",
        link_ok=True,
        replay_ok=True,
        axiom_audit_ok=True,
        axioms=["propext"],
        elapsed_ms=40,
        cache_hit=False,
        kernels_agreeing=[],
        diagnostics=[],
        proof="def sol_1 : LeanAgent.Goals.G_1 := trivial",
    )
    page = render_html(trajectory(verdict=proved))
    assert "<b>proved</b>" in page and "Accepted proof" in page
    assert "def sol_1 : LeanAgent.Goals.G_1 := trivial" in page


def test_the_page_loads_nothing_from_anywhere_else() -> None:
    """One self-contained page: no script, no stylesheet or font from a CDN -- a debugging tool
    has to work on a machine with no network, which is where most of this system runs."""
    page = render_html(trajectory())
    assert "<script" not in page and "http://" not in page and "https://" not in page
