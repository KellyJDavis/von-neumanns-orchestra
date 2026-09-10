"""The executor's `check` screen, and the bug M2.10's miniF2F gate surfaced.

No infrastructure: the `LeanService` is a recording stub, because what is under test is the
executor's *decision*, and a real kernel would only make it slower to check. The real-kernel
evidence that a partial proof actually looks like this lives in `tests/leanserv/test_api.py`,
which pins `/v1/check` reporting `sorryAx` for a body Lean accepted with only a warning.

The bug: an attempt gets exactly one verdict (`verdict.attempt_id` is a primary key), so the
executor screens candidates with `/v1/check` and spends its one `/v1/link` on the first that looks
good. It read `ok` alone -- and a `sorry` in Lean is a *warning*, never an error. `apply?`,
`exact?` and `rw?` routinely leave a partial proof and let error recovery fill the hole with
`sorryAx`, which elaborates with `ok=True`. So the first suggestion tactic in the portfolio
consumed the attempt, the audit rejected it, and every tactic after it was unreachable -- with
`DEFAULT_TACTICS` ordering `exact?`/`apply?`/`rw?` ahead of `linarith`/`nlinarith`/`aesop`, that
is most of the portfolio, on most goals.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from lean_agent_core.actions import Action, Budget, ObligationContext, SubmitProof
from lean_agent_core.enums import ProvenanceClass, VerdictKind
from lean_agent_core.executor import SORRY_AXIOM, PolicyExecutor, TrajectoryStep
from lean_agent_core.protocols import CheckOutcome, LinkOutcome
from lean_agent_core.state import ObligationOutcome

CLEAN_AXIOMS = ("propext", "Classical.choice")


@dataclass
class RecordingLean:
    """A `LeanService` that answers `check` from a script and records every `link`.

    `sorry_tactics` are the ones whose candidate elaborates cleanly but depends on `sorryAx` --
    exactly what a partial `apply?` produces.
    """

    sorry_tactics: frozenset[str]
    failing_tactics: frozenset[str] = frozenset()
    checked: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)

    @staticmethod
    def _tactic_of(development: str) -> str:
        return development.rsplit("intros; ", 1)[-1].split("\n", 1)[0]

    async def check(
        self,
        *,
        base_env_digest: str,
        body: str,
        bundle_sha: str | None = None,
        timeout_ms: int | None = None,
    ) -> CheckOutcome:
        tactic = self._tactic_of(body)
        self.checked.append(tactic)
        if tactic in self.failing_tactics:
            return CheckOutcome(ok=False, diagnostics=(f"{tactic} failed",))
        axioms = (SORRY_AXIOM, *CLEAN_AXIOMS) if tactic in self.sorry_tactics else CLEAN_AXIOMS
        return CheckOutcome(ok=True, axioms=axioms)

    async def link(
        self,
        *,
        attempt_id: uuid.UUID,
        obligation_id: uuid.UUID,
        base_env_digest: str,
        bundle_sha: str,
        goal: str,
        entry: str,
        development: str,
        timeout_ms: int | None = None,
    ) -> LinkOutcome:
        tactic = self._tactic_of(development)
        self.linked.append(tactic)
        # What the real `/v1/link` would say: the kernel accepts the term, and the audit rejects
        # it because `sorryAx` is not in the run's allowlist.
        if tactic in self.sorry_tactics:
            return LinkOutcome(
                kind=VerdictKind.ERRORS,
                link_ok=True,
                replay_ok=True,
                axiom_audit_ok=False,
                axioms=(SORRY_AXIOM,),
                diagnostics=("axiom cone contains sorryAx",),
            )
        return LinkOutcome(
            kind=VerdictKind.PROVED,
            link_ok=True,
            replay_ok=True,
            axiom_audit_ok=True,
            axioms=CLEAN_AXIOMS,
        )


@dataclass
class RecordingTrajectories:
    steps: list[TrajectoryStep] = field(default_factory=list)
    provenance: ProvenanceClass | None = None

    async def write(
        self,
        *,
        attempt_id: uuid.UUID,
        provenance: ProvenanceClass,
        steps: list[TrajectoryStep],
        sampling: dict[str, object] | None = None,
        model_id: str | None = None,
        seed: int | None = None,
    ) -> None:
        self.steps = list(steps)
        self.provenance = provenance


@dataclass(frozen=True)
class ScriptedPortfolio:
    """A stand-in for `SymbolicPortfolio` with the same emitted shape, so `RecordingLean` can
    recover which tactic a development belongs to the same way the real text allows."""

    tactics: tuple[str, ...]
    id: str = "ScriptedPortfolio"
    tools: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()

    @property
    def config_hash(self) -> bytes:
        return b"\x00" * 32

    async def propose(self, ctx: ObligationContext, budget: Budget) -> AsyncIterator[Action]:
        for tactic in self.tactics:
            yield SubmitProof(
                development=f"def sol : {ctx.goal_decl} := by unfold X; intros; {tactic}",
                entry=ctx.entry,
                label=tactic,
                timeout_ms=1000,
            )


def _context(*, allow_sorry: bool = False) -> ObligationContext:
    return ObligationContext(
        obligation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        base_env_digest="ab" * 32,
        bundle_sha="cd" * 32,
        goal_decl="LeanAgent.Goals.G_1",
        goal_src="True",
        entry="LeanAgent.Sol.sol_1",
        allow_sorry=allow_sorry,
    )


def _execute(
    lean: RecordingLean, tactics: tuple[str, ...], *, allow_sorry: bool = False
) -> tuple[ObligationOutcome, RecordingTrajectories]:
    trajectories = RecordingTrajectories()
    ctx = _context(allow_sorry=allow_sorry)
    executor = PolicyExecutor(
        policy=ScriptedPortfolio(tactics=tactics),
        lean=lean,
        trajectories=trajectories,  # type: ignore[arg-type]
        context_loader=lambda _o, _r: _ready((ctx, Budget(attempts_remaining=1))),
    )
    result = asyncio.run(executor.execute(attempt_id=uuid.uuid4(), ctx=ctx, budget=Budget(1)))
    return result.outcome, trajectories


async def _ready(value: tuple[ObligationContext, Budget]) -> tuple[ObligationContext, Budget]:
    return value


def test_a_candidate_that_elaborates_via_sorry_does_not_spend_the_attempt_s_one_link() -> None:
    """The regression. `apply?` elaborates cleanly and is not a proof; the attempt's one `link`
    must go to `linarith`, which is behind it in the portfolio."""
    lean = RecordingLean(sorry_tactics=frozenset({"apply?"}))
    outcome, trajectories = _execute(lean, ("apply?", "linarith"))

    assert lean.checked == ["apply?", "linarith"], "both candidates must be screened"
    assert lean.linked == ["linarith"], (
        "only the genuine candidate may be linked -- linking `apply?` would write this attempt's "
        f"one verdict and end it; linked: {lean.linked}"
    )
    assert outcome is ObligationOutcome.PROVED
    # The skip is recorded rather than silent: a trajectory that showed nothing between `apply?`
    # and `linarith` would make this behaviour invisible to whoever debugs a run later.
    skipped = [s for s in trajectories.steps if s.label == "apply?"]
    assert skipped and skipped[0].ok is False
    assert skipped[0].detail is not None and SORRY_AXIOM in skipped[0].detail


def test_every_tactic_after_a_sorry_producing_one_is_still_reachable() -> None:
    """The shape of the original bug at portfolio scale: with `exact?`/`apply?`/`rw?` all leaving
    partial proofs, the tactics behind them were dead code."""
    suggestion_tactics = frozenset({"exact?", "apply?", "rw?"})
    lean = RecordingLean(
        sorry_tactics=suggestion_tactics,
        failing_tactics=frozenset({"rfl", "decide", "omega"}),
    )
    tactics = ("rfl", "decide", "omega", "exact?", "apply?", "rw?", "linarith")
    outcome, _ = _execute(lean, tactics)

    assert lean.checked == list(tactics)
    assert lean.linked == ["linarith"]
    assert outcome is ObligationOutcome.PROVED


def test_a_run_that_allows_sorry_still_links_a_sorry_candidate() -> None:
    """The screen must say what the audit will say, not something stricter.

    `run.allow_sorry` is a real column that `/v1/link` honours, so on a run that permits `sorryAx`
    the candidate *would* be accepted -- and skipping it here would deny that run a result it
    explicitly asked for. This is the same discipline as the state machine and the privilege model
    being two views of one guarantee: a screen that disagrees with its enforcement point is drift.
    """
    lean = RecordingLean(sorry_tactics=frozenset({"apply?"}))
    outcome, _ = _execute(lean, ("apply?", "linarith"), allow_sorry=True)

    assert lean.checked == ["apply?"], "the screen must not look past the first viable candidate"
    assert lean.linked == ["apply?"]
    # `RecordingLean` still reports the audit rejecting it, because this stub's allowlist is fixed;
    # what is under test is which candidate the executor chose to spend the link on.
    assert outcome is ObligationOutcome.RETRYABLE_FAILURE


def test_a_candidate_that_fails_to_elaborate_is_skipped_as_before() -> None:
    """The pre-existing screen still works: this is the path the sorry check was added beside,
    not a replacement for it."""
    lean = RecordingLean(sorry_tactics=frozenset(), failing_tactics=frozenset({"rfl"}))
    outcome, trajectories = _execute(lean, ("rfl", "decide"))

    assert lean.checked == ["rfl", "decide"]
    assert lean.linked == ["decide"]
    assert outcome is ObligationOutcome.PROVED
    assert [s.label for s in trajectories.steps] == ["rfl", "decide"]
