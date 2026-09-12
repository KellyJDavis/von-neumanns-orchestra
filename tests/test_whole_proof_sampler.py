"""M3.9 -- `WholeProofSampler`, on its own.

No infrastructure. The model output these tests parse is **real**: it is read from the same
recorded Goedel-Prover-V2-8B responses `tests/leanserv/test_whole_proof.py` replays, so the
extraction rule is held to what the prover actually writes rather than to what someone imagined it
would. The end-to-end claim -- that such a sample proves a sealed goal through link, replay and
audit -- lives in that integration test.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from lean_agent_core.actions import (
    Action,
    Budget,
    ObligationContext,
    RequestCompletion,
    SubmitProof,
)
from lean_agent_core.protocols import CheckOutcome, Completion, CompletionResponse, Observation
from lean_agent_core.roles import ModelRole
from lean_agent_policies.whole_proof import (
    GOEDEL_PROVER_V2,
    PromptTemplate,
    ProverFormat,
    StatementLayout,
    WholeProofSampler,
    extract_lean_block,
)

GOEDEL_FIXTURES = Path(__file__).parent / "models" / "data" / "goedel_whole_proof.json"

#: Goedel-Prover-V2's prompt as its authors wrote it (model card; `src/utils.py`). Pinned here as a
#: literal so that editing the asset is a visible, deliberate change: spec §6.6 asks that ported
#: prompts be kept verbatim and any divergence treated as a measured change.
GOEDEL_VERBATIM = (
    "Complete the following Lean 4 code:\n\n```lean4\n$formal_statement```\n\n"
    "Before producing the Lean 4 code to formally prove the given theorem, provide a detailed "
    "proof plan outlining the main proof steps and strategies.\n"
    "The plan should highlight key ideas, intermediate lemmas, and proof structures that will "
    "guide the construction of the final formal proof."
)

CTX = ObligationContext(
    obligation_id=uuid.uuid4(),
    run_id=uuid.uuid4(),
    base_env_digest="ab" * 32,
    bundle_sha="cd" * 32,
    goal_decl="LeanAgent.Goals.G_7",
    goal_src="∀ (n : ℕ), 0 < n → (21 * n + 4).gcd (14 * n + 3) = 1",
    entry="LeanAgent.Sol.sol_7",
    base_env_imports=("Mathlib.Data.Nat.GCD.Basic", "Mathlib.Tactic.Ring"),
)


def _recorded_texts() -> list[str]:
    document = json.loads(GOEDEL_FIXTURES.read_text())
    return [
        choice["text"]
        for interaction in document["interactions"]
        for choice in interaction["response"]["choices"]
    ]


# --------------------------------------------------------------------------------------------
# What the model is shown.
# --------------------------------------------------------------------------------------------


def test_the_prompt_asset_is_goedels_own_text_verbatim() -> None:
    assert PromptTemplate.load(GOEDEL_PROVER_V2).text == GOEDEL_VERBATIM


def test_band_one_is_the_sealed_goal_plus_the_base_env() -> None:
    """Spec §6.6's band 1, "sealed goal, pretty-printed, plus base env" -- as the Lean file the
    prover was trained to complete. The imports are the base env's real ones, in order: a prompt
    that did not say what the goal was elaborated against would leave the model guessing which
    lemmas exist."""
    statement = WholeProofSampler().formal_statement(CTX)
    assert statement == (
        "import Mathlib.Data.Nat.GCD.Basic\nimport Mathlib.Tactic.Ring\n\n"
        "set_option maxHeartbeats 400000\n\n"
        "open BigOperators Real Nat Topology Rat\n\n"
        "theorem G_7 : ∀ (n : ℕ), 0 < n → (21 * n + 4).gcd (14 * n + 3) = 1 := by sorry"
    )


def test_the_statement_shown_is_the_sealed_one() -> None:
    """The model sees the `∀`-closed statement the obligation actually has. That is the only
    statement there is -- the submitted signature is gone once the goal is sealed -- and it is the
    one the kernel will hold the answer to."""
    (message,) = WholeProofSampler().messages(CTX)
    assert message.role == "user"
    assert f"theorem G_7 : {CTX.goal_src} := by sorry```" in message.content
    assert message.content.startswith("Complete the following Lean 4 code:\n\n```lean4\nimport ")


def test_a_universe_polymorphic_goal_names_its_universes_everywhere() -> None:
    ctx = ObligationContext(**{**CTX.__dict__, "level_params": ("u_1",)})
    policy = WholeProofSampler()
    assert "theorem G_7.{u_1} : " in policy.formal_statement(ctx)
    development = policy.development(ctx, "theorem G_7.{u} : True := trivial")
    assert "def sol_7.{u_1} : LeanAgent.Goals.G_7.{u_1} :=" in development
    assert "exact LeanAgent.Sol.G_7.{u_1}" in development


# --------------------------------------------------------------------------------------------
# What is done with what it writes.
# --------------------------------------------------------------------------------------------


def test_the_entry_is_typed_by_the_sealed_constant_never_by_the_models_statement() -> None:
    """§1.1 held at the level of the text: whatever theorem the model restated, the declaration
    that is linked has the sealed constant as its type, and the model's theorem only has to
    inhabit it. A weakened restatement is an `exact` that fails, not a proof of the wrong thing."""
    development = WholeProofSampler().development(CTX, "theorem G_7 : False := by sorry")
    assert (
        "def sol_7 : LeanAgent.Goals.G_7 := by unfold LeanAgent.Goals.G_7; "
        "exact LeanAgent.Sol.G_7" in development
    )
    assert "theorem G_7 : False := by sorry" in development


def test_the_header_the_prompt_showed_is_repeated_in_the_development() -> None:
    """Measured: Goedel's final block restates the theorem and *not* the header, so its proofs
    resolve names through an `open` line it never wrote. The development carries the same one."""
    development = WholeProofSampler().development(CTX, "theorem G_7 : True := trivial")
    assert "open BigOperators Real Nat Topology Rat\n" in development
    assert "set_option maxHeartbeats 400000\n" in development
    assert development.index("open BigOperators") < development.index("theorem G_7")


def test_a_models_own_imports_are_dropped() -> None:
    """The base env is fixed before the model runs, and `import` anywhere but a file's head is a
    parse error -- so a model restating `import Mathlib` would otherwise fail a fine proof."""
    block = "import Mathlib\nimport Aesop\n\ntheorem G_7 : True := trivial"
    development = WholeProofSampler().development(CTX, block)
    assert "import" not in development
    assert "theorem G_7 : True := trivial" in development


def test_every_recorded_sample_yields_its_final_proof_not_its_sketch() -> None:
    """The rule that matters most, against real output. Every recorded Goedel sample writes two
    blocks: a sketch whose steps are `sorry`, then the proof. Taking the first would submit the
    sketch -- which *elaborates*, since a `sorry` is only a warning."""
    texts = _recorded_texts()
    assert texts, "the recorded fixture should contain samples"
    for text in texts:
        blocks = text.count("```lean4\n")
        block = extract_lean_block(text)
        assert block is not None
        assert block.startswith("theorem G_1 : "), block[:80]
        if blocks > 1:
            assert "sorry" not in block, "took the sketch rather than the final proof"


def test_a_block_is_only_read_after_the_models_reasoning() -> None:
    draft = "<think>\n```lean4\ntheorem draft : False := sorry\n```\n</think>\n"
    answer = "```lean4\ntheorem G_1 : True := trivial\n```"
    assert extract_lean_block(draft + answer) == "theorem G_1 : True := trivial"


def test_a_sample_cut_off_mid_thought_has_no_answer() -> None:
    """An unclosed `<think>` is a sample that ran out of tokens; any block in it is a draft."""
    assert extract_lean_block("<think>\n```lean4\ntheorem x : True := trivial\n```") is None


def test_a_sample_with_no_block_has_no_answer() -> None:
    assert extract_lean_block("### Proof\nBy induction on n.") is None
    assert extract_lean_block("```lean4\ntheorem cut : True := by") is None


def test_a_plain_lean_fence_is_accepted() -> None:
    assert extract_lean_block("```lean\ntheorem G_1 : True := trivial\n```") == (
        "theorem G_1 : True := trivial"
    )


# --------------------------------------------------------------------------------------------
# The action stream.
# --------------------------------------------------------------------------------------------


def _completion(text: str) -> Completion:
    return Completion(token_ids=(1,), logprobs=(-0.1,), text=text, finish_reason="stop")


async def _drive(
    policy: WholeProofSampler, budget: Budget, observation: Observation | None
) -> list[Action]:
    stream: AsyncGenerator[Action, Observation | None] = policy.propose(CTX, budget)
    actions: list[Action] = []
    try:
        actions.append(await stream.asend(None))
        actions.append(await stream.asend(observation))
        while True:
            actions.append(await stream.asend(CheckOutcome(ok=False, diagnostics=("error: nope",))))
    except StopAsyncIteration:
        return actions


def test_one_request_then_one_submission_per_sample_that_wrote_a_proof() -> None:
    """One request for *n* samples, not *n* requests: one prefill, and one cache entry holding all
    of them (M3.6). Then each sample with a final block is submitted; one without is not -- its
    tokens are on the trajectory regardless, and the step says how it ended."""
    response = CompletionResponse(
        completions=(
            _completion("```lean4\ntheorem G_7 : True := trivial\n```"),
            _completion("### Plan\nI ran out of tokens before writing"),
            _completion("```lean4\ntheorem G_7 : True := by trivial\n```"),
        ),
        prompt_token_ids=(1, 2, 3),
        model_id="Goedel-LM/Goedel-Prover-V2-8B",
    )
    policy = WholeProofSampler()
    request, *submissions = asyncio.run(_drive(policy, Budget(1), response))

    assert isinstance(request, RequestCompletion)
    assert request.role is ModelRole.PROVER
    assert request.messages == policy.messages(CTX)
    assert request.sampling == {}, "the deployment's configured sampling is the default"
    assert [s.label for s in submissions if isinstance(s, SubmitProof)] == ["sample 0", "sample 2"]
    assert all(isinstance(s, SubmitProof) and s.entry == CTX.entry for s in submissions)


def test_nothing_is_proposed_with_no_attempts_left() -> None:
    async def first() -> list[Action]:
        return [action async for action in WholeProofSampler().propose(CTX, Budget(0))]

    assert asyncio.run(first()) == []


def test_being_sent_something_other_than_the_completion_is_an_error() -> None:
    """A policy that silently proceeded with no samples would record an attempt that "tried and
    failed" when the executor had in fact broken the contract."""
    with pytest.raises(TypeError, match="CompletionResponse"):
        asyncio.run(_drive(WholeProofSampler(), Budget(1), None))


def test_sampling_overrides_are_passed_through_and_hashed() -> None:
    policy = WholeProofSampler(sampling={"n": 2})
    request, *_ = asyncio.run(
        _drive(
            policy, Budget(1), CompletionResponse(completions=(), prompt_token_ids=(), model_id="m")
        )
    )
    assert isinstance(request, RequestCompletion)
    assert request.sampling == {"n": 2}
    assert policy.config_hash != WholeProofSampler().config_hash


# --------------------------------------------------------------------------------------------
# What is recorded about the configuration.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changed",
    [
        {
            "prompt_format": ProverFormat(
                PromptTemplate(name=GOEDEL_PROVER_V2, text=GOEDEL_VERBATIM + " "),
                StatementLayout.GOEDEL_PIPELINE,
            )
        },
        {"prompt_format": ProverFormat.pythagoras_card()},
        {"opens": "open Nat"},
        {"max_heartbeats": 200_000},
        {"check_timeout_ms": 1_000},
    ],
)
def test_the_config_hash_moves_with_everything_that_changes_behaviour(
    changed: dict[str, Any],
) -> None:
    """Including the prompt's *content* under an unchanged name: an edited prompt is a different
    experiment, and a manifest that could not tell would publish it as the same one."""
    assert WholeProofSampler(**changed).config_hash != WholeProofSampler().config_hash


def test_the_prompt_hash_is_in_the_manifests_own_form() -> None:
    hashes = WholeProofSampler().prompt_hashes
    assert hashes == {"prover": f"sha256:{PromptTemplate.load(GOEDEL_PROVER_V2).sha256}"}


def test_the_policy_declares_the_prover_role_and_no_tools() -> None:
    policy = WholeProofSampler()
    assert policy.roles == frozenset({ModelRole.PROVER})
    assert policy.tools == frozenset()


# --------------------------------------------------------------------------------------------
# M3.12: each prover's published prompt, byte for byte, rebuilt by its own code or card.
# --------------------------------------------------------------------------------------------

INFORMAL = "What is the least positive $n$ with $n > 0$? Show that it is 1."
FULL = ObligationContext(
    **{**CTX.__dict__, "base_env_imports": ("Mathlib", "Aesop"), "informal_statement": INFORMAL}
)
#: The published header (DeepSeek-Prover's miniF2F, which Goedel-Prover-V2's `dataset/minif2f.jsonl`
#: carries verbatim), with the one stated change: the heartbeat limit this system actually sets.
HEADER = (
    "import Mathlib\nimport Aesop\n\nset_option maxHeartbeats 0\n\n"
    "open BigOperators Real Nat Topology Rat\n\n"
).replace("maxHeartbeats 0", "maxHeartbeats 400000")
THEOREM = f"theorem G_7 : {CTX.goal_src} := by"
PLAN = (
    "Before producing the Lean 4 code to formally prove the given theorem, provide a detailed "
    "proof plan outlining the main proof steps and strategies.\nThe plan should highlight key "
    "ideas, intermediate lemmas, and proof structures that will guide the construction of the "
    "final formal proof."
)


def test_goedels_default_prompt_is_what_its_released_pipeline_builds() -> None:
    """`prover_inference` from Goedel-Prover-V2's `src/utils.py`, applied to its own dataset's
    `lean4_code` shape (header, informal docstring, statement)."""
    lean4_code = f"{HEADER}/-- {INFORMAL}-/\n{THEOREM} sorry"
    formal_statement = lean4_code.split(":= by")[0] + ":= by sorry"
    prompt = f"Complete the following Lean 4 code:\n\n```lean4\n{formal_statement}```\n\n{PLAN}"
    (message,) = WholeProofSampler().messages(FULL)
    assert (message.role, message.content) == ("user", prompt)


def test_pythagoras_prompt_is_what_its_card_builds() -> None:
    formal_statement = f"""
{HEADER}/-- {INFORMAL}-/
{THEOREM}
  sorry
""".strip()
    prompt = f"""
Complete the following Lean 4 code:

```lean4
{{}}```

{PLAN}
""".strip()
    (message,) = WholeProofSampler(prompt_format=ProverFormat.pythagoras_card()).messages(FULL)
    assert (message.role, message.content) == ("user", prompt.format(formal_statement))


def test_kiminas_prompt_is_what_its_card_builds() -> None:
    header = (
        "import Mathlib\nimport Aesop\nset_option maxHeartbeats 400000\n"
        "open BigOperators Real Nat Topology Rat\n"
    )
    formal_statement = f"{header}/-- {INFORMAL}-/\n{THEOREM}\n"
    prompt = "Think about and solve the following problem step by step in Lean 4."
    prompt += f"\n# Problem:{INFORMAL}"
    prompt += f"\n# Formal statement:\n```lean4\n{formal_statement}\n```\n"
    messages = WholeProofSampler(prompt_format=ProverFormat.kimina_card()).messages(FULL)
    assert [(m.role, m.content) for m in messages] == [
        ("system", "You are an expert in mathematics and Lean 4."),
        ("user", prompt),
    ]


def test_without_an_informal_statement_nothing_is_invented() -> None:
    """No docstring, and Kimina's `# Problem:` left empty: the published shapes, unfilled."""
    ctx = ObligationContext(**{**FULL.__dict__, "informal_statement": None})
    assert "/--" not in WholeProofSampler().formal_statement(ctx)
    _, user = WholeProofSampler(prompt_format=ProverFormat.kimina_card()).messages(ctx)
    assert "\n# Problem:\n# Formal statement:\n" in user.content


def test_the_m3_9_layout_is_byte_identical_to_what_was_recorded() -> None:
    """No docstring even when there is an informal statement, and the newline before the fence --
    what M3.9 shipped, or the M3.9/M3.10 recordings would stop replaying."""
    statement = WholeProofSampler(prompt_format=ProverFormat.m3_9()).formal_statement(FULL)
    assert statement == f"{HEADER}{THEOREM}\n  sorry\n"


def test_each_format_is_a_different_configuration() -> None:
    formats = (
        ProverFormat.goedel_pipeline,
        ProverFormat.pythagoras_card,
        ProverFormat.kimina_card,
        ProverFormat.m3_9,
    )
    assert len({WholeProofSampler(prompt_format=f()).config_hash for f in formats}) == 4
    kimina = WholeProofSampler(prompt_format=ProverFormat.kimina_card()).prompt_hashes
    assert set(kimina) == {"prover", "prover_system"}
