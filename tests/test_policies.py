"""M2.5: `SymbolicPortfolio` and spec §7.1's provenance rule, as pure logic.

No database and no Lean here -- `tests/leanserv/test_null_agent.py` runs the same policy against
a real kernel. What this file pins down is the part that must be true before any of that: the text
a policy emits, the hash that identifies its configuration, and the one rule the executor enforces
about where a trajectory came from.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from lean_agent_core.actions import Budget, ObligationContext, SubmitProof
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.executor import PolicyContractError, resolve_provenance
from lean_agent_policies.symbolic import DEFAULT_TACTICS, SymbolicPortfolio

CTX = ObligationContext(
    obligation_id=uuid.UUID(int=1),
    run_id=uuid.UUID(int=2),
    base_env_digest="ab" * 32,
    bundle_sha="cd" * 32,
    goal_decl="LeanAgent.Goals.G_x",
    goal_src="∀ n : Nat, n + 0 = n",
    entry="LeanAgent.Sol.sol_x",
)


def _proposals(policy: SymbolicPortfolio, budget: Budget) -> list[SubmitProof]:
    async def run() -> list[SubmitProof]:
        out = []
        async for action in policy.propose(CTX, budget):
            assert isinstance(action, SubmitProof)
            out.append(action)
        return out

    return asyncio.run(run())


def test_the_development_never_states_the_goal() -> None:
    """Spec §1.1's core invariant, at the level of the text a policy actually emits: the entry's
    type is the sealed *constant*, and the statement appears nowhere."""
    development = SymbolicPortfolio().development(CTX, "simp")
    assert "def sol_x : LeanAgent.Goals.G_x := by" in development
    assert CTX.goal_src not in development


def test_the_development_unfolds_the_sealed_goal_before_the_tactic() -> None:
    """Without this the tactic faces an opaque `def` and fails for reasons that have nothing to do
    with the mathematics -- `decide` cannot synthesize `Decidable Goals.G_x`, `simp` "made no
    progress", `omega` finds "no usable constraints". Confirmed against the real toolchain."""
    assert "by unfold LeanAgent.Goals.G_x; omega" in SymbolicPortfolio().development(CTX, "omega")


def test_universe_parameters_are_spelled_out_on_both_sides() -> None:
    ctx = ObligationContext(**{**CTX.__dict__, "level_params": ("u_1", "v")})
    development = SymbolicPortfolio().development(ctx, "rfl")
    assert "def sol_x.{u_1, v} : LeanAgent.Goals.G_x.{u_1, v} :=" in development


def test_one_proposal_per_tactic_in_order() -> None:
    policy = SymbolicPortfolio(tactics=("rfl", "simp", "omega"))
    assert [p.label for p in _proposals(policy, Budget(attempts_remaining=1))] == [
        "rfl",
        "simp",
        "omega",
    ]


def test_no_proposals_when_the_attempt_budget_is_gone() -> None:
    """Proposing into an exhausted budget would spend real kernel time on an attempt whose result
    cannot be used."""
    assert _proposals(SymbolicPortfolio(), Budget(attempts_remaining=0)) == []


def test_config_hash_covers_order_and_timeout() -> None:
    """Order decides which tactic proves a goal first and therefore what the trajectory records,
    so two runs over the same tactics in different orders are different configurations -- and
    spec §7.3's manifest must not claim they are the same."""
    a = SymbolicPortfolio(tactics=("rfl", "simp"))
    b = SymbolicPortfolio(tactics=("simp", "rfl"))
    assert a.config_hash != b.config_hash
    assert a.config_hash == SymbolicPortfolio(tactics=("rfl", "simp")).config_hash
    assert (
        a.config_hash != SymbolicPortfolio(tactics=("rfl", "simp"), tactic_timeout_ms=1).config_hash
    )


def test_the_policy_needs_no_model_roles() -> None:
    """Phase 2's "zero model calls anywhere in the codebase", as a property rather than a claim:
    an executor has nothing to ask a model for."""
    assert SymbolicPortfolio().roles == frozenset()
    assert SymbolicPortfolio().tools == frozenset()


def test_the_default_portfolio_is_spec_s_list() -> None:
    for tactic in (
        "exact?",
        "apply?",
        "rw?",
        "aesop",
        "simp_all",
        "omega",
        "bv_decide",
        "decide",
        "linarith",
        "nlinarith",
        "polyrith",
        "norm_num",
        "field_simp",
    ):
        assert tactic in DEFAULT_TACTICS


def test_zero_completions_is_symbolic() -> None:
    assert resolve_provenance(None, 0) is ProvenanceClass.SYMBOLIC
    # Even with a backend registered: a policy that could have asked a model and did not produced
    # a symbolic trajectory.
    assert resolve_provenance(ProvenanceClass.OPEN_WEIGHTS, 0) is ProvenanceClass.SYMBOLIC


def test_completions_take_the_backend_s_provenance() -> None:
    assert resolve_provenance(ProvenanceClass.OPEN_WEIGHTS, 3) is ProvenanceClass.OPEN_WEIGHTS
    assert (
        resolve_provenance(ProvenanceClass.CLOSED_API_EVAL_ONLY, 1)
        is ProvenanceClass.CLOSED_API_EVAL_ONLY
    )


def test_symbolic_is_refused_on_any_attempt_with_completions() -> None:
    """Spec §7.1, verbatim: "`symbolic` is refused by the executor on any attempt with a nonzero
    completion count. A model-guided tactic choice is not a symbolic trajectory however few tokens
    it used." This is what keeps `SymbolicPortfolio`'s output genuinely unencumbered rather than
    merely believed to be."""
    with pytest.raises(PolicyContractError, match="not a symbolic trajectory"):
        resolve_provenance(ProvenanceClass.SYMBOLIC, 1)


def test_completions_with_no_attributable_provenance_raise() -> None:
    """`trajectory.provenance` is NOT NULL with no default precisely so this cannot be papered
    over -- an unattributable trajectory must not reach the corpus at all."""
    with pytest.raises(PolicyContractError, match="no model provenance"):
        resolve_provenance(None, 2)
