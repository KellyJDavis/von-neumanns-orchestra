"""The executor: the only thing that performs a policy's proposed side effects (spec §6.6, §7.1).

Spec states the division in one line -- "The executor performs side effects, not the policy. That
is what keeps trajectories replayable and the tool allowlist enforceable." This module is the
other half of that sentence. It consumes `Action`s, does the ones it can, refuses the ones it must,
records what happened as a `trajectory`, and hands M2.4's control loop an `AttemptResult`.

Three things are enforced here and nowhere else:

* **The tool allowlist.** `CallTool` is checked against `Policy.tools` before anything happens.
* **Provenance (§7.1).** `symbolic` is refused on any attempt with a nonzero completion count --
  "a model-guided tactic choice is not a symbolic trajectory however few tokens it used". This is
  what makes `SymbolicPortfolio`'s output usable as unencumbered training data rather than merely
  believed to be.
* **Stopping.** The action stream is abandoned the moment a proof lands, which for a timed
  portfolio is the difference between one tactic's cost and twelve.

**Screen with `check`, spend the one `link`.** `verdict.attempt_id` is a primary key, so an attempt
gets at most one verdict ever (spec §5.1: "verdict (0..1, written by leanserv only)"), while a
portfolio makes many submissions inside one attempt. Every `SubmitProof` is therefore screened
through `/v1/check` -- which elaborates, caches, and writes nothing -- and only a candidate that
elaborates cleanly is committed to `/v1/link`, once. This was not the first design: linking every
submission blew up on the second tactic with a `verdict_pkey` violation, which is the schema
saying what the endpoint table already said.

A candidate that passes `check` and then fails `link` ends the attempt. That combination is
information, not noise -- `check` only elaborates, while `link` re-checks in the kernel with forced
options, replays, and audits, so a gap between them is exactly the environment-hacking or
axiom-cone case those passes exist to catch.

**Observations flow back into the policy.** A policy's `propose` is driven with `asend`, and after
each action the executor sends in what it saw -- the `CompletionResponse` for a `RequestCompletion`,
the `CheckOutcome` for a screened-out `SubmitProof` (see `protocols.Observation`). Until M3.9 there
was no channel back at all, which was fine for a portfolio that decides everything up front and is
impossible for a policy that has to read the samples it asked for.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lean_agent_core.actions import (
    Abandon,
    Action,
    Budget,
    CallTool,
    Decompose,
    ObligationContext,
    RequestCompletion,
    SubmitProof,
)
from lean_agent_core.blobs import store_or_inline, to_bytea
from lean_agent_core.codecs import (
    encode_trajectory_logprobs,
    encode_trajectory_token_ids,
)
from lean_agent_core.enums import ProvenanceClass
from lean_agent_core.protocols import (
    BlobStore,
    Completion,
    CompletionResponse,
    LeanService,
    Observation,
    Policy,
)
from lean_agent_core.roles import ModelRole
from lean_agent_core.scheduler import ClaimedAttempt
from lean_agent_core.state import ObligationOutcome
from lean_agent_core.worker import AttemptResult, AttemptRunner, AttemptSpend

#: Loads spec §6.4's `ctx` for one obligation. A callable rather than a class so a caller can bind
#: its own session factory (or, in a test, hand over a fixed context) without either side knowing
#: how the other gets its database connection.
ContextLoader = Callable[[uuid.UUID, uuid.UUID], Awaitable[tuple[ObligationContext, Budget]]]


class CompletionService(Protocol):
    """What the executor needs to turn a `RequestCompletion` into tokens.

    A protocol in `core` rather than a direct dependency on `lean_agent_models.router`, for the
    reason `LeanService` is one: `core` holds the executor, `models` depends on `core`, and the
    other direction would be a cycle. It is also the narrower contract -- the executor needs a
    role resolved and a request performed, not the router's configuration, manifest or lifecycle.

    `provenance_for` is separate from `complete` because §7.1 is specific: `trajectory.provenance`
    is "derived from `ModelBackend.provenance` at model registration time. It is never asserted by
    whatever code is writing the trajectory row." The executor asks which provenance served a role;
    it never chooses one.
    """

    async def complete(self, request: RequestCompletion) -> CompletionResponse: ...

    def provenance_for(self, role: ModelRole) -> ProvenanceClass: ...


#: Lean's axiom for `sorry`. Named directly, which is safe here for the reason `Audit.lean` gives:
#: it is Lean's one stable, version-independent axiom name -- unlike the `native_decide` axioms
#: spec §4.4 warns about, which were silently renamed in 4.29 and are why the *audit* is an
#: allowlist rather than a name list. This is not the audit: it is a screen deciding whether a
#: candidate is worth spending the attempt's one verdict on, and the run's real allowlist is still
#: applied by `/v1/link`. A candidate rejected here would have been rejected there.
SORRY_AXIOM = "sorryAx"

logger = logging.getLogger(__name__)


class PolicyContractError(Exception):
    """A policy proposed something it is not allowed to propose -- a tool outside its own
    allowlist, or an action no executor implements.

    Not an `InfraError`: nothing about the infrastructure failed, and treating it as unbudgeted
    would let a misconfigured policy retry forever at no cost (M2.4's finding, in a second form).
    It escapes as an ordinary exception, which the control loop charges as a failed attempt.
    """


def resolve_provenance(
    model_provenance: ProvenanceClass | None, completions: int
) -> ProvenanceClass:
    """Spec §7.1's rule, applied where the rule says it is applied: in the executor.

    > `symbolic` is refused by the executor on any attempt with a nonzero completion count. A
    > model-guided tactic choice is not a symbolic trajectory however few tokens it used.

    The rule is one-directional and implemented as such: nonzero completions means *not*
    `symbolic`, and the provenance must come from the `ModelBackend` that served them. Zero
    completions is `symbolic` even if a backend was registered -- a policy that could have asked a
    model and did not produced a symbolic trajectory.

    Raises rather than falling back when completions were served with no provenance to attribute
    them to. Spec makes `trajectory.provenance` `NOT NULL` with no default precisely so this
    cannot be papered over: an unattributable trajectory must not reach the corpus at all, and the
    exporter "raises (does not filter)" for the same reason one milestone later.
    """
    if completions == 0:
        return ProvenanceClass.SYMBOLIC
    if model_provenance is None:
        raise PolicyContractError(
            f"{completions} completions were served with no model provenance to attribute them "
            "to; `symbolic` is refused on any attempt with a nonzero completion count (spec §7.1)"
        )
    if model_provenance is ProvenanceClass.SYMBOLIC:
        raise PolicyContractError(
            "a backend declaring `symbolic` provenance served completions; a model-guided tactic "
            "choice is not a symbolic trajectory however few tokens it used (spec §7.1)"
        )
    return model_provenance


@dataclass(frozen=True)
class TrajectoryStep:
    """One proposed action and what came of it. Serialized into `trajectory.steps_blob`, which for
    a symbolic run *is* the training data spec §7.1 says this policy exists to produce -- so the
    label that identifies which portfolio member ran matters as much as the outcome."""

    label: str
    action: str
    ok: bool
    detail: str | None = None


class _Proposals:
    """A policy's `propose` generator, driven the way `protocols.Observation` describes.

    An `async for` over this is an `async for` over the policy's actions, except that each step sends
    in whatever the previous action produced (`observe`), or `None` when it produced nothing a policy
    can act on. There is one pending observation and the next step consumes it, so an observation
    can neither leak into a later step nor be delivered twice.
    """

    def __init__(self, generator: AsyncGenerator[Action, Observation | None]) -> None:
        self._generator = generator
        self._pending: Observation | None = None

    def observe(self, observation: Observation) -> None:
        self._pending = observation

    def __aiter__(self) -> _Proposals:
        return self

    async def __anext__(self) -> Action:
        pending, self._pending = self._pending, None
        return await self._generator.asend(pending)

    async def aclose(self) -> None:
        await self._generator.aclose()


class TrajectoryWriter:
    """Writes the one `trajectory` row an attempt produces.

    `app` holds `INSERT` on `trajectory` directly (unlike `verdict`), because a trajectory is a
    record of what the agent did rather than a judgement about whether it worked -- and nothing
    downstream trusts it for acceptance. Its integrity requirement is different in kind: spec §7.1
    cares that `provenance` is *true*, which `resolve_provenance` decides, not that the row is
    unforgeable.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], blob_store: BlobStore
    ) -> None:
        self._sessions = session_factory
        self._blobs = blob_store

    async def write(
        self,
        *,
        attempt_id: uuid.UUID,
        provenance: ProvenanceClass,
        steps: list[TrajectoryStep],
        sampling: dict[str, object] | None = None,
        model_id: str | None = None,
        seed: int | None = None,
        prompt_token_ids: tuple[int, ...] = (),
        completions: tuple[Completion, ...] = (),
        model_weights_hash: str | None = None,
        tokenizer_revision: str | None = None,
    ) -> None:
        """Write the one `trajectory` row an attempt produces.

        The token-id and logprob columns have existed since M1.5 and were left NULL through all of
        Phase 2, which was correct then -- a symbolic attempt has no tokens. They are filled from
        M3.7 on, and they are the reason `provenance` matters: §9's own table lists "token ids +
        logprobs at generation" under *cannot be recomputed correctly later*, so an attempt whose
        completions were not recorded here is one that can never be trained on or replayed.

        `model_weights_hash` and `tokenizer_revision` go beside them because ids alone are not
        interpretable: the same ids mean different text under a different tokenizer, and the same
        prompt gives different ids under different weights (§7.3's manifest names both for exactly
        this reason).
        """
        payload = json.dumps([step.__dict__ for step in steps], sort_keys=True).encode()
        steps_blob = to_bytea(await store_or_inline(self._blobs, payload, "application/json"))

        token_ids_blob: bytes | None = None
        logprobs_blob: bytes | None = None
        if completions:
            token_ids_blob = to_bytea(
                await store_or_inline(
                    self._blobs,
                    encode_trajectory_token_ids(prompt_token_ids, completions),
                    "application/json",
                )
            )
            logprobs_blob = to_bytea(
                await store_or_inline(
                    self._blobs,
                    encode_trajectory_logprobs(completions),
                    "application/json",
                )
            )

        async with self._sessions() as session:
            await session.execute(
                text(
                    "INSERT INTO trajectory (attempt_id, provenance, model_id, "
                    "model_weights_hash, tokenizer_revision, sampling, seed, steps_blob, "
                    "token_ids_blob, logprobs_blob, n_steps) VALUES "
                    "(:id, CAST(:prov AS provenance_class), :model, :weights, :tokenizer, "
                    "CAST(:sampling AS jsonb), :seed, :steps, :tokens, :logprobs, :n)"
                ),
                {
                    "id": attempt_id,
                    "prov": provenance.value,
                    "model": model_id,
                    "weights": model_weights_hash,
                    "tokenizer": tokenizer_revision,
                    "sampling": json.dumps(sampling or {}, sort_keys=True),
                    "seed": seed,
                    "steps": steps_blob,
                    "tokens": token_ids_blob,
                    "logprobs": logprobs_blob,
                    "n": len(steps),
                },
            )
            await session.commit()


class PolicyExecutor:
    """Runs one policy against one obligation and reports what happened.

    Constructed per worker, not per attempt: it holds only collaborators. Use `runner(...)` to get
    the `AttemptRunner` M2.4's `Worker` takes.
    """

    def __init__(
        self,
        *,
        policy: Policy,
        lean: LeanService,
        trajectories: TrajectoryWriter,
        context_loader: ContextLoader,
        completions: CompletionService | None = None,
    ) -> None:
        self._policy = policy
        self._lean = lean
        self._trajectories = trajectories
        self._load_context = context_loader
        #: `None` for a policy that asks for none -- `SymbolicPortfolio` is the whole of Phase 2 --
        #: which is what keeps "zero model calls anywhere in the codebase" true by construction
        #: rather than by discipline: with no service wired up, a `RequestCompletion` cannot be
        #: performed even by mistake.
        self._completions = completions

    async def execute(
        self, *, attempt_id: uuid.UUID, ctx: ObligationContext, budget: Budget
    ) -> AttemptResult:
        """Consume the policy's proposals until one proves the goal or the stream ends.

        Every submission's kernel time is accumulated whether it succeeded or not: a portfolio
        that burned eleven tactics before the twelfth worked genuinely spent all twelve tactics'
        kernel budget, and recording only the winner's would understate the run's cost by roughly
        the portfolio size.
        """
        steps: list[TrajectoryStep] = []
        kernel_ms = 0
        completions = 0
        outcome = ObligationOutcome.RETRYABLE_FAILURE
        model_provenance: ProvenanceClass | None = None
        model_id: str | None = None
        model_weights_hash: str | None = None
        tokenizer_revision: str | None = None
        sampling: dict[str, object] | None = None
        seed: int | None = None
        prompt_token_ids: tuple[int, ...] = ()
        sampled: list[Completion] = []
        tokens_in = 0
        tokens_out = 0

        proposals = _Proposals(self._policy.propose(ctx, budget))
        try:
            async for action in proposals:
                match action:
                    case SubmitProof():
                        screened = await self._lean.check(
                            base_env_digest=ctx.base_env_digest,
                            body=action.development,
                            bundle_sha=ctx.bundle_sha,
                            timeout_ms=action.timeout_ms,
                        )
                        kernel_ms += screened.elapsed_ms
                        if not screened.ok:
                            steps.append(
                                TrajectoryStep(
                                    label=action.label,
                                    action="SubmitProof/check",
                                    ok=False,
                                    detail=_first(screened.diagnostics),
                                )
                            )
                            proposals.observe(screened)
                            continue
                        if SORRY_AXIOM in screened.axioms and not ctx.allow_sorry:
                            # `ok` is true and the candidate is still not a proof. A `sorry` is a
                            # *warning* in Lean, so a body that leaves the goal open elaborates
                            # cleanly -- and the search tactics do this constantly: `apply?` reports
                            # "found a partial proof", emits its suggestions, and lets Lean's error
                            # recovery fill the hole with `sorryAx`.
                            #
                            # Screening on `ok` alone therefore spent this attempt's *one* verdict on
                            # a candidate the audit was always going to reject, and the `break` below
                            # then ended the attempt -- so with `exact?`/`apply?`/`rw?` sitting ahead
                            # of `linarith` in the portfolio, the tactics that would actually have
                            # closed the goal were unreachable. Found by M2.10's miniF2F gate; every
                            # tactic after the first suggestion tactic was dead code before this.
                            #
                            # Gated on the run's own `allow_sorry` so this screen says exactly what
                            # the audit will say: a run that permits `sorryAx` would have accepted
                            # this candidate, and skipping it here would deny that run a result it
                            # asked for.
                            steps.append(
                                TrajectoryStep(
                                    label=action.label,
                                    action="SubmitProof/check",
                                    ok=False,
                                    detail=f"elaborated but depends on {SORRY_AXIOM}",
                                )
                            )
                            proposals.observe(screened)
                            continue
                        result = await self._lean.link(
                            attempt_id=attempt_id,
                            obligation_id=ctx.obligation_id,
                            base_env_digest=ctx.base_env_digest,
                            bundle_sha=ctx.bundle_sha,
                            goal=ctx.goal_decl,
                            entry=action.entry,
                            development=action.development,
                            timeout_ms=action.timeout_ms,
                        )
                        kernel_ms += result.elapsed_ms
                        steps.append(
                            TrajectoryStep(
                                label=action.label,
                                action="SubmitProof",
                                ok=result.proved,
                                detail=None if result.proved else _first(result.diagnostics),
                            )
                        )
                        if result.proved:
                            outcome = ObligationOutcome.PROVED
                        # Either way the attempt is over: the one verdict this attempt may ever have
                        # has now been written, so a further submission could not be linked even if a
                        # later tactic would have worked. A retry is a new attempt, which is exactly
                        # what `verdict.attempt_id` being a primary key is telling us to do.
                        break
                    case Abandon():
                        steps.append(
                            TrajectoryStep(
                                label="abandon", action="Abandon", ok=False, detail=action.reason
                            )
                        )
                        break
                    case CallTool():
                        # Checked before anything happens: an allowlist enforced after the call is not
                        # an allowlist. `ToolClient` does not exist yet, so every tool is outside it.
                        if action.tool not in self._policy.tools:
                            raise PolicyContractError(
                                f"policy {self._policy.id} proposed tool {action.tool!r}, which is "
                                f"outside its own allowlist {sorted(self._policy.tools)}"
                            )
                        raise PolicyContractError(
                            f"tool {action.tool!r} is allowlisted but no ToolClient is wired up yet"
                        )
                    case Decompose():
                        raise PolicyContractError(
                            "Decompose is not executable yet: it needs each child sealed, its edges "
                            "inserted and its bundle materialized (M2.6/M2.7), not just /v1/decompose"
                        )
                    case RequestCompletion():
                        if action.role not in self._policy.roles:
                            raise PolicyContractError(
                                f"policy {self._policy.id} requested role {action.role.value!r}, "
                                f"which is outside its own declared roles "
                                f"{sorted(r.value for r in self._policy.roles)}. `Policy.roles` is "
                                "what a router checks before the run starts, so a policy that asks "
                                "for more than it declared makes that check meaningless."
                            )
                        if self._completions is None:
                            raise PolicyContractError(
                                f"policy {self._policy.id} requested completions from role "
                                f"{action.role.value!r}, but this executor has no completion service. "
                                "A policy declaring roles needs one wired up."
                            )

                        sample = await self._completions.complete(action)
                        completions += 1
                        # Every sample's cost is counted, not just a winner's -- the same principle as
                        # kernel time above, and the reason §7.5 reports "pass@k with the budget that
                        # produced it".
                        tokens_in += len(sample.prompt_token_ids)
                        tokens_out += sum(len(c.token_ids) for c in sample.completions)
                        sampled.extend(sample.completions)
                        prompt_token_ids = sample.prompt_token_ids or prompt_token_ids
                        model_id = sample.model_id
                        model_weights_hash = sample.model_weights_hash or model_weights_hash
                        tokenizer_revision = sample.tokenizer_revision or tokenizer_revision
                        model_provenance = self._completions.provenance_for(action.role)
                        # What was actually sent, when the service says: the policy's own
                        # overrides are only part of it, and for a policy relying on the
                        # deployment's configured sampling they are empty (see
                        # `CompletionResponse.sampling`).
                        if sample.sampling is not None:
                            sampling = sample.sampling.canonical()
                        else:
                            sampling = dict(action.sampling) or sampling
                        if sample.seed is not None:
                            seed = sample.seed
                        elif action.seed is not None:
                            seed = action.seed
                        steps.append(
                            TrajectoryStep(
                                label=action.role.value,
                                action="RequestCompletion",
                                ok=True,
                                detail=_sample_detail(sample),
                            )
                        )
                        proposals.observe(sample)
        finally:
            # `break` out of an `async for` does not close the generator underneath it; that
            # is left to garbage collection, which would run a policy's cleanup at some
            # arbitrary later point on whatever loop happens to be current. Closed here, once.
            await proposals.aclose()

        provenance = resolve_provenance(model_provenance, completions)
        await self._trajectories.write(
            attempt_id=attempt_id,
            provenance=provenance,
            steps=steps,
            sampling=sampling,
            model_id=model_id,
            seed=seed,
            prompt_token_ids=prompt_token_ids,
            completions=tuple(sampled),
            model_weights_hash=model_weights_hash,
            tokenizer_revision=tokenizer_revision,
        )
        return AttemptResult(
            outcome=outcome,
            spend=AttemptSpend(tokens_in=tokens_in, tokens_out=tokens_out, kernel_ms=kernel_ms),
        )

    def runner(self) -> AttemptRunner:
        """Adapt to M2.4's `AttemptRunner`: the loop hands over a claimed attempt, and everything
        else -- the sealed goal, the bundle, the budget -- is loaded here."""

        async def run(claimed: ClaimedAttempt) -> AttemptResult:
            ctx, budget = await self._load_context(claimed.obligation_id, claimed.run_id)
            return await self.execute(attempt_id=claimed.attempt_id, ctx=ctx, budget=budget)

        return run


def _sample_detail(response: CompletionResponse) -> str:
    """How many samples came back and how each one ended.

    `finish_reason` is the first thing to read when a sampling policy proves nothing: `length`
    across the board means the token budget cut every sample off before it reached a proof, which
    no amount of resampling fixes -- and without it in the step, that reads exactly like a model
    that tried and failed.
    """
    reasons = Counter(c.finish_reason for c in response.completions)
    ends = ", ".join(f"{reason}×{count}" for reason, count in sorted(reasons.items()))
    cached = "; cached" if response.cache_hit else ""
    return f"{len(response.completions)} sample(s) from {response.model_id}; finish {ends}{cached}"


def _first(diagnostics: tuple[str, ...]) -> str | None:
    """The head of the diagnostics, truncated. Spec §6.6: "never summarize kernel output --
    truncate instead ... keep the error head". A trajectory step is a label, not a transcript;
    the full diagnostics live on the `verdict` row leanserv wrote."""
    if not diagnostics:
        return None
    head = diagnostics[0]
    return head if len(head) <= 500 else head[:500] + f"… [{len(head) - 500} more bytes]"


async def load_context(
    session_factory: async_sessionmaker[AsyncSession],
    obligation_id: uuid.UUID,
    run_id: uuid.UUID,
) -> tuple[ObligationContext, Budget]:
    """Spec §6.4's `load_context`: band 1, plus the run's rules a policy's screen must agree with.

    Rewritten in M3.9, when `WholeProofSampler` became its first caller. It had never had one --
    every integration test carried its own hand-written loader -- which is how two bugs survived
    from M2.5: `bundle_sha` was read out of `obligation.admission`, which ingestion never writes it
    into (M2.6 gave it a column of its own), so every context came back with `bundle_sha=""` and
    no bundle to link against; and `run.allow_sorry` was not read at all, so a run permitting
    `sorry` got the stricter screen.

    `base_env_imports` comes from the base env's recipe: band 1 is "sealed goal, pretty-printed,
    **plus base env**", and a prompt that shows a model the goal without saying what it was
    elaborated against leaves it guessing which lemmas exist.

    `level_params` is left empty because nothing persists it: ingestion seals a goal with its
    universe parameters but stores neither a column nor an admission entry for them. Harmless for
    every goal so far (miniF2F has no universe-polymorphic statement) and recorded as a known gap
    rather than papered over with a read of a key that is never written.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT o.base_env_digest, o.bundle_sha, o.goal_src, o.decl_name, "
                    "o.budget_attempts - o.spent_attempts, "
                    "r.budget_tokens, r.budget_kernel_ms, r.allow_sorry, b.recipe "
                    "FROM obligation o JOIN run r ON r.id = o.run_id "
                    "JOIN base_env b ON b.digest = o.base_env_digest WHERE o.id = :id"
                ),
                {"id": obligation_id},
            )
        ).one()
    (
        base_env_digest,
        bundle_sha,
        goal_src,
        decl_name,
        attempts_left,
        tokens,
        kernel_ms,
        allow_sorry,
        recipe,
    ) = row
    entry = decl_name.replace("LeanAgent.Goals.G_", "LeanAgent.Sol.sol_", 1)
    return (
        ObligationContext(
            obligation_id=obligation_id,
            run_id=run_id,
            base_env_digest=bytes(base_env_digest).hex(),
            bundle_sha=bytes(bundle_sha).hex(),
            goal_decl=decl_name,
            goal_src=goal_src,
            entry=entry,
            allow_sorry=bool(allow_sorry),
            base_env_imports=tuple(str(module) for module in recipe.get("imports", [])),
        ),
        Budget(
            attempts_remaining=int(attempts_left),
            tokens_remaining=tokens,
            kernel_ms_remaining=kernel_ms,
        ),
    )
