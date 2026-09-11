"""M3.7 -- the executor performing `RequestCompletion`, and what lands on the trajectory.

No infrastructure: the `CompletionService` is a recording stub, because what is under test is the
executor's bookkeeping and §7.1's provenance rule, not HTTP. The real service is exercised against
recorded vLLM bytes in `tests/models/test_completions.py`, and the trajectory columns are checked
against a real PostgreSQL in `tests/db/test_trajectory.py`.

The thing worth watching here is that a model call is *recorded* completely. §9 lists token ids and
logprobs among what "cannot be recomputed correctly later", so an attempt whose completions were
not written down is one that can never be trained on or replayed -- and nothing about the run would
look wrong at the time.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from lean_agent_core.actions import (
    Action,
    Budget,
    Message,
    ObligationContext,
    RequestCompletion,
)
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.executor import PolicyContractError, PolicyExecutor, TrajectoryStep
from lean_agent_core.protocols import Completion, CompletionResponse, SamplingParams
from lean_agent_core.roles import ModelRole
from lean_agent_core.state import ObligationOutcome

SAMPLE = Completion(
    token_ids=(13, 358, 2776),
    logprobs=(-1.44, -1.98, -0.88),
    text=". I'm",
    finish_reason="length",
)


@dataclass
class RecordingCompletions:
    """A `CompletionService` that answers from a script and records what it was asked."""

    provenance: ProvenanceClass = ProvenanceClass.OPEN_WEIGHTS
    samples: tuple[Completion, ...] = (SAMPLE,)
    requests: list[RequestCompletion] = field(default_factory=list)
    #: What the service reports it actually sent, as `RoutedCompletions` does after merging the
    #: deployment's configured sampling with the policy's overrides. `None` models a service that
    #: does not say, which leaves the executor recording the policy's request.
    effective: SamplingParams | None = None
    effective_seed: int | None = None

    async def complete(self, request: RequestCompletion) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse(
            completions=self.samples,
            prompt_token_ids=(9707, 11, 1879),
            model_id="Goedel-LM/Goedel-Prover-V2-8B",
            model_weights_hash="w-abc",
            tokenizer_revision="t-def",
            elapsed_ms=17,
            sampling=self.effective,
            seed=self.effective_seed,
        )

    def provenance_for(self, role: ModelRole) -> ProvenanceClass:
        return self.provenance


@dataclass
class RecordingTrajectories:
    written: dict[str, object] = field(default_factory=dict)

    async def write(self, **kwargs: object) -> None:
        self.written = kwargs


@dataclass(frozen=True)
class ScriptedPolicy:
    """Proposes one `RequestCompletion` and stops."""

    roles: frozenset[ModelRole] = frozenset({ModelRole.PROVER})
    requested_role: ModelRole = ModelRole.PROVER
    id: str = "ScriptedPolicy"
    tools: frozenset[str] = frozenset()
    sampling: dict[str, object] = field(default_factory=dict)
    seed: int | None = None

    @property
    def config_hash(self) -> bytes:
        return b"\x00" * 32

    async def propose(self, ctx: ObligationContext, budget: Budget) -> AsyncIterator[Action]:
        yield RequestCompletion(
            role=self.requested_role,
            messages=(Message(role="user", content="Prove 2 + 2 = 4."),),
            sampling=dict(self.sampling),
            seed=self.seed,
        )


def _ctx() -> ObligationContext:
    return ObligationContext(
        obligation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        base_env_digest="ab" * 32,
        bundle_sha="cd" * 32,
        goal_decl="LeanAgent.Goals.G_1",
        goal_src="True",
        entry="LeanAgent.Sol.sol_1",
    )


def _execute(
    policy: ScriptedPolicy,
    completions: RecordingCompletions | None,
) -> tuple[object, RecordingTrajectories]:
    trajectories = RecordingTrajectories()
    ctx = _ctx()
    executor = PolicyExecutor(
        policy=policy,  # type: ignore[arg-type]
        lean=object(),  # type: ignore[arg-type]
        trajectories=trajectories,  # type: ignore[arg-type]
        context_loader=lambda _o, _r: _ready((ctx, Budget(attempts_remaining=1))),
        completions=completions,  # type: ignore[arg-type]
    )
    result = asyncio.run(executor.execute(attempt_id=uuid.uuid4(), ctx=ctx, budget=Budget(1)))
    return result, trajectories


async def _ready(value: tuple[ObligationContext, Budget]) -> tuple[ObligationContext, Budget]:
    return value


# --------------------------------------------------------------------------------------------
# What gets recorded.
# --------------------------------------------------------------------------------------------


def test_a_completion_is_recorded_with_its_tokens_and_logprobs() -> None:
    """The whole point of the milestone. §9 lists these among what cannot be recomputed later, so
    an attempt that called a model and did not write them down is unusable for training or replay
    -- and nothing at the time would look wrong."""
    _, trajectories = _execute(ScriptedPolicy(), RecordingCompletions())

    written = trajectories.written
    exchanges = written["exchanges"]
    assert isinstance(exchanges, tuple)
    (exchange,) = exchanges
    assert exchange.completions == (SAMPLE,)
    assert exchange.prompt_token_ids == (9707, 11, 1879)
    assert written["model_id"] == "Goedel-LM/Goedel-Prover-V2-8B"
    # Ids alone are not interpretable: the same ids mean different text under a different
    # tokenizer, and the same prompt gives different ids under different weights (§7.3).
    assert written["model_weights_hash"] == "w-abc"
    assert written["tokenizer_revision"] == "t-def"


def test_token_counts_are_charged_to_the_attempt() -> None:
    """Every sample's cost, not just a winner's -- the same principle as kernel time, and the
    reason §7.5 reports "pass@k with the budget that produced it"."""
    result, _ = _execute(ScriptedPolicy(), RecordingCompletions(samples=(SAMPLE, SAMPLE, SAMPLE)))
    assert result.spend.tokens_in == 3  # type: ignore[attr-defined]
    assert result.spend.tokens_out == 9  # type: ignore[attr-defined]


def test_the_step_records_which_role_was_asked() -> None:
    """A trajectory viewer (§7.4) reads these, and "a model was called" without saying which role
    would make a multi-role policy's trace unreadable."""
    _, trajectories = _execute(ScriptedPolicy(), RecordingCompletions())
    steps = trajectories.written["steps"]
    assert isinstance(steps, list)
    step = steps[0]
    assert isinstance(step, TrajectoryStep)
    assert (step.label, step.action, step.ok) == ("prover", "RequestCompletion", True)
    assert step.detail is not None and "1 sample(s)" in step.detail


# --------------------------------------------------------------------------------------------
# §7.1's provenance rule, exercised with completions for the first time.
# --------------------------------------------------------------------------------------------


def test_provenance_comes_from_the_backend_that_served_the_completion() -> None:
    """§7.1: "derived from `ModelBackend.provenance` at model registration time. It is never
    asserted by whatever code is writing the trajectory row." Phase 2 only ever exercised the
    zero-completion branch of this rule; this is the other one."""
    _, trajectories = _execute(
        ScriptedPolicy(), RecordingCompletions(provenance=ProvenanceClass.OPEN_WEIGHTS)
    )
    assert trajectories.written["provenance"] is ProvenanceClass.OPEN_WEIGHTS

    _, closed = _execute(
        ScriptedPolicy(), RecordingCompletions(provenance=ProvenanceClass.CLOSED_API_EVAL_ONLY)
    )
    assert closed.written["provenance"] is ProvenanceClass.CLOSED_API_EVAL_ONLY


def test_a_backend_claiming_symbolic_provenance_is_refused() -> None:
    """ "A model-guided tactic choice is not a symbolic trajectory however few tokens it used."
    Letting this through would put model output into the unencumbered corpus."""
    with pytest.raises(PolicyContractError, match="not a symbolic trajectory"):
        _execute(ScriptedPolicy(), RecordingCompletions(provenance=ProvenanceClass.SYMBOLIC))


# --------------------------------------------------------------------------------------------
# What the executor refuses.
# --------------------------------------------------------------------------------------------


def test_a_policy_asking_for_a_role_it_did_not_declare_is_refused() -> None:
    """`Policy.roles` is what the router checks before the run starts (M3.5). A policy that asks
    for more than it declared would make that check meaningless."""
    policy = ScriptedPolicy(roles=frozenset({ModelRole.PROVER}), requested_role=ModelRole.CRITIC)
    with pytest.raises(PolicyContractError, match="outside its own declared roles"):
        _execute(policy, RecordingCompletions())


def test_a_completion_request_without_a_service_is_refused() -> None:
    """This is what keeps "zero model calls anywhere in the codebase" true by construction rather
    than by discipline: with nothing wired up, a `RequestCompletion` cannot be performed even by
    mistake."""
    with pytest.raises(PolicyContractError, match="no completion service"):
        _execute(ScriptedPolicy(), None)


# --------------------------------------------------------------------------------------------
# Sampling and seed.
# --------------------------------------------------------------------------------------------


def test_the_policys_sampling_and_seed_reach_the_service_and_the_trajectory() -> None:
    """`trajectory.sampling` and `seed` are what make a run reproducible from its record (§7.3),
    so both have to be what was actually asked for."""
    policy = ScriptedPolicy(sampling={"temperature": 0.8, "n": 4}, seed=1234)
    service = RecordingCompletions()
    _, trajectories = _execute(policy, service)

    (asked,) = service.requests
    assert asked.sampling == {"temperature": 0.8, "n": 4}
    assert asked.seed == 1234
    (exchange,) = trajectories.written["exchanges"]  # type: ignore[misc]
    assert exchange.sampling == {"temperature": 0.8, "n": 4}
    assert exchange.seed == 1234


def test_a_symbolic_attempt_still_writes_no_token_columns() -> None:
    """The Phase 2 shape has to keep working unchanged: a policy that requests nothing records no
    completions, and `resolve_provenance` calls that `symbolic`."""

    @dataclass(frozen=True)
    class SilentPolicy(ScriptedPolicy):
        async def propose(self, ctx: ObligationContext, budget: Budget) -> AsyncIterator[Action]:
            return
            yield  # pragma: no cover - makes this an async generator

    result, trajectories = _execute(SilentPolicy(roles=frozenset()), None)

    assert trajectories.written["exchanges"] == ()
    assert trajectories.written["model_id"] is None
    assert trajectories.written["provenance"] is ProvenanceClass.SYMBOLIC
    assert result.outcome is ObligationOutcome.RETRYABLE_FAILURE  # type: ignore[attr-defined]


def test_the_trajectory_records_the_sampling_that_was_sent_not_only_the_overrides() -> None:
    """M3.9's finding. A policy relying on the deployment's configured sampling asks for no
    overrides, so recording the *request* stored `sampling = {}` and `seed = NULL` for a run that
    sampled at temperature 0.8 under seed 1234 -- a trajectory that could not say how it was
    produced. When the service reports what it sent, that is what is recorded."""
    configured = SamplingParams(temperature=0.8, top_p=0.95, max_tokens=4096, n=4)
    service = RecordingCompletions(effective=configured, effective_seed=1234)
    _, trajectories = _execute(ScriptedPolicy(), service)

    (exchange,) = trajectories.written["exchanges"]  # type: ignore[misc]
    assert exchange.sampling == configured.canonical()
    assert exchange.seed == 1234


def test_each_request_is_recorded_with_its_own_prompt() -> None:
    """A policy that asks twice -- a repair after a failed sample -- gets two exchanges, each with
    the prompt its completions actually answered. Before M3.10 the second request's prompt
    overwrote the first, and every completion was stored against it."""

    @dataclass
    class Answering(RecordingCompletions):
        async def complete(self, request: RequestCompletion) -> CompletionResponse:
            self.requests.append(request)
            prompt = tuple(range(len(self.requests) * 3))
            return CompletionResponse(
                completions=self.samples, prompt_token_ids=prompt, model_id="m"
            )

    @dataclass(frozen=True)
    class TwiceAsking(ScriptedPolicy):
        async def propose(self, ctx: ObligationContext, budget: Budget) -> AsyncIterator[Action]:
            for turn in ("first", "second"):
                yield RequestCompletion(
                    role=ModelRole.PROVER,
                    messages=(Message(role="user", content=turn),),
                    sampling={"n": 1},
                )

    _, trajectories = _execute(TwiceAsking(), Answering())
    first, second = trajectories.written["exchanges"]  # type: ignore[misc]
    assert first.prompt_token_ids == (0, 1, 2)
    assert second.prompt_token_ids == (0, 1, 2, 3, 4, 5)
    assert first.completions == second.completions == (SAMPLE,)
