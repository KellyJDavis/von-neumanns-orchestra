"""M3.10 -- a real kernel's error positions land on the model's own code.

`RepairLoop` shows a prover *where* in its own code each error is, and every piece of that is a
mapping someone could get wrong: the kernel positions a diagnostic in the development, the model's
code begins after a header it never wrote, and `render_errors` maps one to the other. A mistake
there is silent in the worst way -- every `<error>` marker lands on the wrong line, the prompt still
looks plausible, and a recording made from it would freeze the mistake. So this runs a real
`leankernel serve` over a development the policy built and checks the marker is on the token the
kernel actually rejected.

An `Init` worker, for speed. The sampler's `open` line is narrowed to `Nat` accordingly:
Goedel's default opens `Real`, `Topology` and friends, and `open` of a namespace the environment
does not have is a hard error (M3.9).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from lean_agent_core.actions import ObligationContext
from lean_agent_policies.feedback import render_errors
from lean_agent_policies.whole_proof import WholeProofSampler
from lean_agent_serv.repl import ReplWorker

CTX = ObligationContext(
    obligation_id=uuid.uuid4(),
    run_id=uuid.uuid4(),
    base_env_digest="00",
    bundle_sha="00",
    goal_decl="LeanAgent.Goals.G_1",
    goal_src="∀ (n : Nat), 0 < n → n + n = 2 * n",
    entry="LeanAgent.Sol.sol_1",
)

#: What a prover might write: a correct theorem with one wrong tactic three lines into the proof,
#: preceded by a helper lemma so the error is not simply on the model's first line.
BLOCK = (
    "theorem helper (n : Nat) : n + n = 2 * n := by omega\n"
    "\n"
    "theorem G_1 : ∀ (n : Nat), 0 < n → n + n = 2 * n := by\n"
    "  intro n hn\n"
    "  exact no_such_lemma n"
)

#: The sealed goal, declared inline: this test is about positions in a `check`, not about link,
#: so it does not need a materialized bundle.
GOAL = (
    "namespace LeanAgent.Goals\n"
    "def G_1 : Sort _ := ∀ (n : Nat), 0 < n → n + n = 2 * n\n"
    "end LeanAgent.Goals\n"
)


def test_the_error_marker_lands_on_the_token_the_kernel_rejected(
    lake_project_dir: Path,
) -> None:
    sampler = WholeProofSampler(opens="open Nat")
    header, code, footer = sampler.development_parts(CTX, BLOCK)

    async def check() -> tuple[str, ...]:
        worker = await ReplWorker.spawn(lake_project_dir, ("Init",))
        try:
            result = await worker.check(GOAL + header + code + footer)
        finally:
            await worker.close()
        assert not result.ok
        return result.diagnostics

    diagnostics = asyncio.run(check())
    # The inline goal adds three lines ahead of the development, so the model's code starts that
    # much further down in *this* check than in a real one, where the goal is imported.
    rendered = render_errors(
        code, header.count("\n") + GOAL.count("\n"), diagnostics, message_budget_tokens=10_000
    )

    assert "  exact <error>no_such_lemma</error> n\n" in rendered, rendered
    assert "Unknown identifier `no_such_lemma`" in rendered
    # The four lines before it are the model's own, helper lemma included.
    assert "theorem helper (n : Nat) : n + n = 2 * n := by omega\n" in rendered
