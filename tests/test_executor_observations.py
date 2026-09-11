"""M3.9 -- the executor sends what it observed back into the policy.

No infrastructure: the collaborators are recording stubs, because what is under test is the
channel -- what reaches a policy's `propose` generator after each action, and when the generator
is closed -- not HTTP or a kernel. `tests/leanserv/test_whole_proof.py` drives the same channel
through the real services.

Until M3.9 there was no channel: `propose` was consumed with a plain `async for`, which is enough
for a portfolio that decides every proposal up front and useless for a policy that has to read the
samples it asked for. The contract now is `protocols.Observation`, and each test below pins one
clause of it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from lean_agent_core.actions import (
    Action,
    Budget,
    Message,
    ObligationContext,
    RequestCompletion,
    SubmitProof,
)
from lean_agent_core.enums import ProvenanceClass, VerdictKind
from lean_agent_core.executor import PolicyExecutor, TrajectoryStep
from lean_agent_core.protocols import (
    CheckOutcome,
    Completion,
    CompletionResponse,
    LinkOutcome,
    Observation,
)
from lean_agent_core.roles import ModelRole

RESPONSE = CompletionResponse(
    completions=(
        Completion(token_ids=(1, 2), logprobs=(-0.5, -0.25), text="ok", finish_reason="stop"),
        Completion(token_ids=(3,), logprobs=(-1.0,), text="cut off", finish_reason="length"),
    ),
    prompt_token_ids=(7, 8, 9),
    model_id="Goedel-LM/Goedel-Prover-V2-8B",
)


@dataclass
class Service:
    async def complete(self, request: RequestCompletion) -> CompletionResponse:
        return RESPONSE

    def provenance_for(self, role: ModelRole) -> ProvenanceClass:
        return ProvenanceClass.OPEN_WEIGHTS


@dataclass
class Lean:
    """`check` fails any body containing "bad"; `link` accepts everything it is given."""

    linked: list[str] = field(default_factory=list)

    async def check(self, *, body: str, **_: object) -> CheckOutcome:
        if "bad" in body:
            return CheckOutcome(ok=False, diagnostics=("error: bad proof",))
        return CheckOutcome(ok=True, axioms=("propext",))

    async def link(self, *, development: str, **_: object) -> LinkOutcome:
        self.linked.append(development)
        return LinkOutcome(
            kind=VerdictKind.PROVED, link_ok=True, replay_ok=True, axiom_audit_ok=True
        )


@dataclass
class Trajectories:
    steps: list[TrajectoryStep] = field(default_factory=list)

    async def write(self, *, steps: list[TrajectoryStep], **_: object) -> None:
        self.steps = list(steps)


@dataclass(frozen=True)
class Listening:
    """Asks for samples, then submits `script` in order, recording every value sent back and
    whether its own cleanup ran."""

    script: tuple[str, ...]
    seen: list[Observation | None] = field(default_factory=list)
    closed: list[bool] = field(default_factory=list)
    id: str = "Listening"
    tools: frozenset[str] = frozenset()
    roles: frozenset[ModelRole] = frozenset({ModelRole.PROVER})

    @property
    def config_hash(self) -> bytes:
        return b"\x00" * 32

    async def propose(
        self, ctx: ObligationContext, budget: Budget
    ) -> AsyncGenerator[Action, Observation | None]:
        try:
            request = RequestCompletion(role=ModelRole.PROVER, messages=(Message("user", "?"),))
            self.seen.append((yield request))
            for body in self.script:
                submission = SubmitProof(development=body, entry=ctx.entry, label=body)
                self.seen.append((yield submission))
        finally:
            self.closed.append(True)


def _run(policy: Listening, lean: Lean | None = None) -> tuple[Lean, Trajectories, list[bool]]:
    """Returns `closed` as it stood the moment `execute` returned -- read *inside* the loop.

    Not after `asyncio.run`: that calls `shutdown_asyncgens` on the way out, which closes every
    abandoned generator, so a test reading `closed` afterwards would pass whether or not the
    executor ever closed anything.
    """
    lean = lean or Lean()
    trajectories = Trajectories()
    ctx = ObligationContext(
        obligation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        base_env_digest="ab" * 32,
        bundle_sha="cd" * 32,
        goal_decl="LeanAgent.Goals.G_1",
        goal_src="True",
        entry="LeanAgent.Sol.sol_1",
    )
    executor = PolicyExecutor(
        policy=policy,
        lean=lean,  # type: ignore[arg-type]
        trajectories=trajectories,  # type: ignore[arg-type]
        context_loader=lambda _o, _r: _ready((ctx, Budget(1))),
        completions=Service(),
    )

    async def main() -> list[bool]:
        await executor.execute(attempt_id=uuid.uuid4(), ctx=ctx, budget=Budget(1))
        return list(policy.closed)

    closed_at_return = asyncio.run(main())
    return lean, trajectories, closed_at_return


async def _ready(value: tuple[ObligationContext, Budget]) -> tuple[ObligationContext, Budget]:
    return value


def test_the_policy_is_shown_the_completion_it_asked_for() -> None:
    """`response = yield RequestCompletion(...)` -- the whole reason the channel exists. The very
    object the service returned, every sample of it, not a summary."""
    policy = Listening(script=())
    _run(policy)
    assert policy.seen == [RESPONSE]


def test_a_screened_out_submission_sends_its_check_outcome_back() -> None:
    """A candidate that fails `/v1/check` does not end the attempt, so the policy is still
    running -- and is told why, diagnostics included. That is what `RepairLoop` will feed back."""
    policy = Listening(script=("bad", "good"))
    lean, _, _ = _run(policy)

    response, screened, *rest = policy.seen
    assert response is RESPONSE
    assert isinstance(screened, CheckOutcome)
    assert screened.ok is False and screened.diagnostics == ("error: bad proof",)
    # The generator is closed right after "good" links, so nothing is ever sent for it.
    assert rest == []
    assert lean.linked == ["good"]


def test_the_generator_is_closed_once_a_link_ends_the_attempt() -> None:
    """`break` out of an `async for` leaves the generator to garbage collection, which would run
    the policy's cleanup at an arbitrary later moment on whatever loop is current. The executor
    closes it itself, so cleanup has run by the time the attempt returns."""
    policy = Listening(script=("good", "never proposed"))
    lean, _, closed = _run(policy)
    assert closed == [True]
    assert lean.linked == ["good"], "nothing after the linked candidate may be asked for"


def test_the_generator_is_closed_when_it_runs_out() -> None:
    policy = Listening(script=("bad",))
    _, _, closed = _run(policy)
    assert closed == [True]


def test_the_step_says_how_each_sample_ended() -> None:
    """`finish_reason` in the step, because a sampling policy that proves nothing because every
    sample hit `max_tokens` looks exactly like one that tried and failed unless the trajectory says
    otherwise -- and the two call for opposite fixes."""
    _, trajectories, _ = _run(Listening(script=()))
    (step,) = trajectories.steps
    assert step.action == "RequestCompletion"
    assert step.detail == "2 sample(s) from Goedel-LM/Goedel-Prover-V2-8B; finish length×1, stop×1"


def test_steps_record_what_was_submitted_and_everything_the_kernel_said() -> None:
    """M3.11. Before this a failed candidate's text was stored nowhere, and only the head of its
    first diagnostic survived -- so a trajectory could say sample 2 failed and not what sample 2
    was, or why. Each request step also names the exchange it produced."""
    _, trajectories, _ = _run(Listening(script=("bad", "good")))
    request, screened, linked = trajectories.steps

    assert (request.action, request.exchange) == ("RequestCompletion", 0)
    assert (screened.development, screened.diagnostics) == ("bad", ("error: bad proof",))
    assert linked.action == "SubmitProof" and linked.development == "good"
