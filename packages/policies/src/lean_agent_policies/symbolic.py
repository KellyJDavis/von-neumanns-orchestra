"""`SymbolicPortfolio` (spec §6.6): a timed portfolio of Lean's own tactics, zero tokens.

Spec's table gives the members: `exact?`, `apply?`, `rw?`, `aesop`, `simp_all`, `omega`,
`bv_decide`, `decide`, `linarith`, `nlinarith`, `polyrith`, `norm_num`, `field_simp`.

This policy is the whole reason Phase 2 can claim "zero model calls anywhere in the codebase":
`roles` is empty, so an executor has nothing to ask a model for, and §7.1's provenance rule then
records every trajectory it produces as `symbolic`. Spec is explicit that this is not merely a
baseline -- "`SymbolicPortfolio` matters beyond being a baseline: it produces unencumbered
training data at zero token cost", because distilling a closed model produces encumbered weights.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from lean_agent_core.actions import Action, Budget, ObligationContext, SubmitProof
from lean_agent_core.protocols import Observation, Policy
from lean_agent_core.roles import ModelRole

#: Spec §6.6's portfolio, in a deliberate order: cheap and decisive first, expensive and general
#: last. The executor stops at the first success, so ordering is the entire cost model -- a
#: portfolio that ran `polyrith` before `rfl` would pay for the search on goals `rfl` closes.
#:
#: Availability depends on the base environment and that is fine, not a bug to guard against:
#: `omega`/`decide`/`simp` are core, while `aesop`, `linarith`, `polyrith`, `norm_num` and
#: `field_simp` need Mathlib. A tactic the environment does not have fails with "unknown tactic",
#: which is just another failed portfolio member -- confirmed against a real `Init`-only worker.
DEFAULT_TACTICS: tuple[str, ...] = (
    "rfl",
    "decide",
    "simp",
    "simp_all",
    "omega",
    "norm_num",
    "exact?",
    "apply?",
    "rw?",
    "linarith",
    "nlinarith",
    "field_simp",
    "aesop",
    "bv_decide",
    "polyrith",
)

#: Per-tactic wallclock, not per-attempt: spec calls this a "timed portfolio", and the point is
#: that one pathological tactic cannot eat the whole attempt. `polyrith` and `nlinarith` can run
#: for a very long time on goals they will never close.
DEFAULT_TACTIC_TIMEOUT_MS = 10_000


@dataclass(frozen=True)
class SymbolicPortfolio:
    """Proposes one `SubmitProof` per tactic, in order, until the executor stops asking.

    Implements `lean_agent_core.protocols.Policy`. Frozen, so `config_hash` cannot drift from the
    configuration it was computed for.
    """

    tactics: tuple[str, ...] = DEFAULT_TACTICS
    tactic_timeout_ms: int = DEFAULT_TACTIC_TIMEOUT_MS

    id: str = "SymbolicPortfolio"
    tools: frozenset[str] = frozenset()
    roles: frozenset[ModelRole] = frozenset()

    @property
    def config_hash(self) -> bytes:
        """Hashes exactly what changes behaviour -- the tactic list, in order, and the timeout.

        Order is included because it decides which tactic proves a goal first and therefore what
        the trajectory records; two runs over the same tactics in different orders are genuinely
        different configurations and spec §7.3's manifest should not claim otherwise.
        """
        payload = json.dumps(
            {"tactics": list(self.tactics), "tactic_timeout_ms": self.tactic_timeout_ms},
            sort_keys=True,
        ).encode()
        return hashlib.sha256(payload).digest()

    def development(self, ctx: ObligationContext, tactic: str) -> str:
        """The Lean source for one portfolio member.

        Three things about this text are load-bearing, and all three were settled against the real
        toolchain rather than assumed:

        1. **The entry's type is the sealed constant itself** (`def sol : LeanAgent.Goals.G_x`),
           never a restatement of the goal. That is spec §1.1's "the agent never writes the goal
           statement", made true of the text a policy emits rather than only of what the kernel
           later checks.
        2. **`unfold` comes first.** The sealed goal is a `def`, so a tactic facing it sees an
           opaque constant: `decide` reports "failed to synthesize Decidable Goals.G_arith", `simp`
           "made no progress", `omega` "no usable constraints". `unfold <goal>` fixes all three.
           (`with_unfolding_all` does *not* -- it changes what defeq checks may unfold, not the
           goal's syntactic form. `delta` works and `unfold` is the more idiomatic spelling.)
           Unfolding is not the agent stating the goal: it names a constant and asks Lean to expand
           it, and the kernel still type-checks the result against `G_x` at link time.
        3. **Universe parameters are spelled out** when the goal has them, on both the entry and
           the goal reference, since neither is inferable from the other here.

        4. **`intros` comes between the unfold and the tactic**, and it is not cosmetic. Sealing
           turns a submitted `theorem f (x : T) (h : P) : C` into the closed statement
           `∀ (x : T), P → C` -- the binders that were in the theorem's *signature* become part of
           the goal. So a tactic that would have faced `C` with `x` and `h` already in context now
           faces a `∀`, and `omega`/`linarith`/`rfl` simply fail on that. Measured over miniF2F:
           without `intros` the portfolio closes only the hypothesis-free problems, which is an
           artifact of this system's own sealing rather than anything about the goals. `intros` on
           a goal with no binders is a no-op, so this cannot cost a proof.

        `linter.defProp` is silenced because a `def` whose type is a `Prop` draws "use `theorem`
        instead" on every single Prop goal -- pure noise in the diagnostics of an attempt that
        succeeded. Linters are advisory and cannot affect link, replay or audit; this is not the
        option deny-list of spec §7.2, which is about options that change what is *checked*.

        The matching `linter.unusedTactic` (which `intros` trips on a hypothesis-free goal, with
        "`intros` does nothing") is deliberately **not** silenced: that linter ships with Mathlib,
        not with core, and `set_option` on an unknown option is a hard *error*, so setting it would
        break every attempt against an `Init`-only base env. An unknown *tactic* degrades to one
        failed portfolio member; an unknown *option* fails the whole development. Confirmed against
        a real `Init` worker, after adding it broke every non-Mathlib test in the suite.
        """
        universes = "" if not ctx.level_params else ".{" + ", ".join(ctx.level_params) + "}"
        goal = f"{ctx.goal_decl}{universes}"
        entry = ctx.entry.rsplit(".", 1)[-1]
        namespace = ctx.entry.rsplit(".", 1)[0]
        return (
            "set_option linter.defProp false\n"
            f"namespace {namespace}\n"
            f"def {entry}{universes} : {goal} := by unfold {ctx.goal_decl}; intros; {tactic}\n"
            f"end {namespace}"
        )

    async def propose(
        self, ctx: ObligationContext, budget: Budget
    ) -> AsyncGenerator[Action, Observation | None]:
        """Yield one submission per tactic, lazily.

        A generator rather than a list so the executor's "stop at the first success" actually
        saves the remaining tactics' kernel time -- for a fifteen-member portfolio on a goal
        `rfl` closes, that is the difference between one check and fifteen.
        """
        if budget.attempts_remaining <= 0:
            return
        for tactic in self.tactics:
            yield SubmitProof(
                development=self.development(ctx, tactic),
                entry=ctx.entry,
                label=tactic,
                timeout_ms=self.tactic_timeout_ms,
            )


def _conforms_to_policy_protocol() -> Policy:
    """Structural conformance to `Policy`, checked by `mypy --strict` rather than asserted in
    prose. Without a typed use somewhere in `packages/`, a drift between the protocol and this
    class would only surface at the executor's first call."""
    return SymbolicPortfolio()
