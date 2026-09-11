"""M3.10 -- `RepairLoop`'s action stream, driven by hand.

No infrastructure: the test plays executor, answering each `RequestCompletion` with a scripted
`CompletionResponse` and each `SubmitProof` with a scripted `CheckOutcome` -- exactly what the real
executor sends back (`protocols.Observation`). What is under test is the conversation the policy
builds and when it asks for what. `tests/leanserv/test_repair_positions.py` holds the error
positions to a real kernel, and `tests/leanserv/test_whole_proof.py` drives the whole pipeline.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from lean_agent_core.actions import (
    Action,
    Budget,
    ObligationContext,
    RequestCompletion,
    SubmitProof,
)
from lean_agent_core.protocols import CheckOutcome, Completion, CompletionResponse
from lean_agent_core.roles import ModelRole
from lean_agent_policies.repair import (
    DEFAULT_PROMPT_BUDGET_TOKENS,
    GOEDEL_CORRECTION,
    RepairLoop,
)
from lean_agent_policies.whole_proof import PromptTemplate, WholeProofSampler

#: Goedel-Prover-V2's correction turn with its two placeholders, pinned as a literal so editing
#: the asset is a visible, deliberate change (spec §6.6: divergence is a measured change).
GOEDEL_CORRECTION_VERBATIM = (
    "The proof (Round $round) is not correct. Following is the compilation error message, where "
    "we use <error></error> to signal the position of the error.\n\n$errors\n\n"
    "Before producing the Lean 4 code to formally prove the given theorem, provide a detailed "
    "analysis of the error message."
)

CTX = ObligationContext(
    obligation_id=uuid.uuid4(),
    run_id=uuid.uuid4(),
    base_env_digest="ab" * 32,
    bundle_sha="cd" * 32,
    goal_decl="LeanAgent.Goals.G_7",
    goal_src="∀ (n : ℕ), 0 < n → n = n",
    entry="LeanAgent.Sol.sol_7",
    base_env_imports=("Mathlib.Tactic.Ring",),
)


ONE_ATTEMPT = Budget(attempts_remaining=1)


def answer(tactic: str) -> str:
    """A prover answer in Goedel's shape: a plan, a sketch block, then the final block."""
    return (
        "### Plan\nIntroduce, then close.\n\n```lean4\ntheorem G_7 : ∀ (n : ℕ), 0 < n → n = n := by"
        "\n  sorry\n```\n\n### Proof\n\n```lean4\ntheorem G_7 : ∀ (n : ℕ), 0 < n → n = n := by\n"
        f"  intro n hn\n  {tactic}\n```"
    )


def response(*texts: str, prompt_tokens: int = 100) -> CompletionResponse:
    return CompletionResponse(
        completions=tuple(
            Completion(token_ids=(1,) * 50, logprobs=(-0.1,) * 50, text=t, finish_reason="stop")
            for t in texts
        ),
        prompt_token_ids=(0,) * prompt_tokens,
        model_id="Goedel-LM/Goedel-Prover-V2-8B",
    )


#: The model's `{tactic}` sits on line 3 of its code, and the default header is five lines, so the
#: kernel reports it on development line 8 -- the offset `test_repair_positions.py` checks for real.
def failed(tactic: str) -> CheckOutcome:
    return CheckOutcome(
        ok=False,
        diagnostics=(f"<input>:8:2-8:{2 + len(tactic)}: error: unknown tactic\n",),
    )


@dataclass
class Executor:
    """Plays the executor: `respond` answers requests, `judge` answers submissions -- returning
    `None` for a submission that would link, which ends the attempt."""

    respond: Callable[[RequestCompletion], CompletionResponse]
    judge: Callable[[SubmitProof], CheckOutcome | None]
    actions: list[Action] = field(default_factory=list)

    def run(self, policy: RepairLoop, budget: Budget = ONE_ATTEMPT) -> list[Action]:
        async def drive() -> None:
            stream = policy.propose(CTX, budget)
            sent: Any = None
            try:
                while True:
                    action = await stream.asend(sent)
                    self.actions.append(action)
                    if isinstance(action, RequestCompletion):
                        sent = self.respond(action)
                    else:
                        assert isinstance(action, SubmitProof)
                        sent = self.judge(action)
                        if sent is None:
                            return
            except StopAsyncIteration:
                return
            finally:
                await stream.aclose()

        asyncio.run(drive())
        return self.actions


def requests(actions: list[Action]) -> list[RequestCompletion]:
    return [a for a in actions if isinstance(a, RequestCompletion)]


def labels(actions: list[Action]) -> list[str]:
    return [a.label for a in actions if isinstance(a, SubmitProof)]


# --------------------------------------------------------------------------------------------
# The conversation.
# --------------------------------------------------------------------------------------------


def test_the_correction_asset_is_goedels_own_text_verbatim() -> None:
    assert PromptTemplate.load(GOEDEL_CORRECTION).text == GOEDEL_CORRECTION_VERBATIM


def test_a_failed_sample_is_shown_its_own_answer_and_the_kernels_errors() -> None:
    """Goedel's shape: `[prompt, the failed answer, the errors]`, the failed answer verbatim --
    plan, sketch and all -- as the model wrote it."""
    first = answer("omega_x")
    replies = iter([response(first), response(answer("rfl"))])
    executor = Executor(
        respond=lambda _: next(replies),
        judge=lambda s: failed("omega_x") if s.label == "sample 0" else None,
    )
    actions = executor.run(RepairLoop(rounds=1))

    opening, repair = requests(actions)
    assert opening.messages == WholeProofSampler().messages(CTX)
    assert [m.role for m in repair.messages] == ["user", "assistant", "user"]
    assert repair.messages[0] == opening.messages[0]
    assert repair.messages[1].content == first
    feedback = repair.messages[2].content
    assert feedback.startswith("The proof (Round 0) is not correct.")
    # The kernel reported development line 8; that is line 3 of the model's own code.
    assert "  <error>omega_x</error>\n" in feedback
    assert "\nError Message: unknown tactic\n" in feedback
    assert labels(actions) == ["sample 0", "sample 0 repair 1"]


def test_the_first_request_uses_the_deployments_sampling_and_a_repair_asks_for_one() -> None:
    """Configuration is the default and a policy narrows it (M3.7): the opening request overrides
    nothing, and a repair asks for one new sample per failed one, as Goedel's pipeline does."""
    replies = iter([response(answer("bad")), response(answer("rfl"))])
    actions = Executor(
        respond=lambda _: next(replies),
        judge=lambda s: failed("bad") if s.label == "sample 0" else None,
    ).run(RepairLoop(rounds=1))
    opening, repair = requests(actions)
    assert opening.sampling == {}
    assert repair.sampling == {"n": 1}


def test_a_second_round_carries_the_whole_history_and_numbers_the_round() -> None:
    replies = iter([response(answer("a")), response(answer("b")), response(answer("rfl"))])
    executor = Executor(
        respond=lambda _: next(replies),
        judge=lambda s: None if s.label == "sample 0 repair 1 repair 2" else failed("x"),
    )
    actions = executor.run(RepairLoop(rounds=2))

    _, first_repair, second_repair = requests(actions)
    assert [m.role for m in second_repair.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert second_repair.messages[:3] == first_repair.messages
    assert second_repair.messages[4].content.startswith("The proof (Round 1) is not correct.")
    # Labels carry the whole lineage, which stays unambiguous when a repair asks for more than one
    # sample and the chain branches.
    assert labels(actions) == ["sample 0", "sample 0 repair 1", "sample 0 repair 1 repair 2"]


def test_repairs_run_breadth_first() -> None:
    """Every first sample is checked before any repair is asked for, and every chain's first repair
    before any second -- a later sample that would prove the goal must not wait behind two rounds
    of repairing an earlier one."""
    replies = iter([response(answer("a"), answer("b"))] + [response(answer("c")) for _ in range(4)])
    actions = Executor(respond=lambda _: next(replies), judge=lambda _: failed("x")).run(
        RepairLoop(rounds=2)
    )
    assert labels(actions) == [
        "sample 0",
        "sample 1",
        "sample 0 repair 1",
        "sample 1 repair 1",
        "sample 0 repair 1 repair 2",
        "sample 1 repair 1 repair 2",
    ]


def test_a_link_ends_everything() -> None:
    """The attempt's one verdict: once a candidate is linked, nothing further is asked for."""
    replies = iter([response(answer("a"), answer("rfl"))])
    actions = Executor(
        respond=lambda _: next(replies),
        judge=lambda s: failed("a") if s.label == "sample 0" else None,
    ).run(RepairLoop())
    assert labels(actions) == ["sample 0", "sample 1"]
    assert len(requests(actions)) == 1


def test_a_sample_that_wrote_no_proof_is_neither_submitted_nor_repaired() -> None:
    """There are no kernel errors to show for code that was never written."""
    replies = iter([response("### Plan\nI ran out of tokens")])
    actions = Executor(respond=lambda _: next(replies), judge=lambda _: failed("x")).run(
        RepairLoop()
    )
    assert labels(actions) == []
    assert len(requests(actions)) == 1


def test_a_chain_whose_next_prompt_would_not_fit_is_not_repaired() -> None:
    """The history grows by a whole answer and a page of errors each round, and a prompt past the
    server's context is a rejected request rather than a worse repair."""
    # Just under the budget, so the next round's answer and errors push it over -- relative, so the
    # test keeps meaning this when the default moves (it was 12,288 until M3.12).
    near_budget = DEFAULT_PROMPT_BUDGET_TOKENS - 88
    replies = iter([response(answer("a"), prompt_tokens=near_budget)])
    actions = Executor(respond=lambda _: next(replies), judge=lambda _: failed("a")).run(
        RepairLoop()
    )
    assert labels(actions) == ["sample 0"]
    assert len(requests(actions)) == 1


def test_zero_rounds_is_the_whole_proof_sampler() -> None:
    replies = iter([response(answer("a"), answer("b"))])
    actions = Executor(respond=lambda _: next(replies), judge=lambda _: failed("x")).run(
        RepairLoop(rounds=0)
    )
    assert labels(actions) == ["sample 0", "sample 1"]
    assert len(requests(actions)) == 1


def test_nothing_is_proposed_with_no_attempts_left() -> None:
    actions = Executor(respond=lambda _: response(), judge=lambda _: None).run(
        RepairLoop(), Budget(0)
    )
    assert actions == []


def test_being_sent_the_wrong_observation_is_an_error() -> None:
    """A submission's answer must be its `CheckOutcome`; carrying on with anything else would
    build a repair prompt from nothing."""
    replies = iter([response(answer("a"))])
    with pytest.raises(TypeError, match="CheckOutcome"):
        Executor(
            respond=lambda _: next(replies),
            judge=lambda _: response(),  # type: ignore[arg-type,return-value]
        ).run(RepairLoop())


# --------------------------------------------------------------------------------------------
# Configuration.
# --------------------------------------------------------------------------------------------


def test_the_models_code_starts_five_lines_into_the_development() -> None:
    """The offset every error position is mapped through. Pinned, because changing the header
    without noticing would shift every `<error>` marker onto the wrong line."""
    header, code, _ = WholeProofSampler().development_parts(CTX, "theorem G_7 : True := trivial")
    assert header.count("\n") == 5
    assert code == "theorem G_7 : True := trivial"


@pytest.mark.parametrize(
    "changed",
    [
        {"rounds": 1},
        {"max_errors": 4},
        {"prompt_budget_tokens": 8_000},
        {"kernel_fraction": 0.5},
        {"repair_sampling": {"n": 2}},
        {"correction": PromptTemplate(name=GOEDEL_CORRECTION, text="Round $round: $errors")},
        {"sampler": WholeProofSampler(max_heartbeats=200_000)},
    ],
)
def test_the_config_hash_moves_with_everything_that_changes_behaviour(
    changed: dict[str, Any],
) -> None:
    assert RepairLoop(**changed).config_hash != RepairLoop().config_hash


def test_both_prompts_are_in_the_manifest() -> None:
    hashes = RepairLoop().prompt_hashes
    assert set(hashes) == {"prover", "repair"}
    assert hashes["repair"] == f"sha256:{PromptTemplate.load(GOEDEL_CORRECTION).sha256}"


def test_the_policy_declares_the_prover_role_and_no_tools() -> None:
    policy = RepairLoop()
    assert policy.roles == frozenset({ModelRole.PROVER})
    assert policy.tools == frozenset()
