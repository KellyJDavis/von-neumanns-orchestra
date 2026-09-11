"""`RepairLoop` (spec §6.6): sample, check, feed the kernel's diagnostics back, resample.

`WholeProofSampler` plus a second chance for every sample that failed: the model is shown its own
failed answer and the errors the kernel reported against it, and asked again. The shape is
Goedel-Prover-V2's own self-correction -- the conversation grows `[prompt, failed answer, errors]`,
each failed sample is corrected on its own with one new sample, for two rounds -- because that is
the shape the prover was trained to repair in, and a repair prompt it has never seen is a
different, worse model.

**What the model is shown is band 2, under band 2's rules** (spec §6.6): the kernel's messages,
localized to the model's own code, truncated and never summarized, inside a hard share of the
prompt budget. `feedback.render_errors` does the rendering; this module decides what to render
and whether it still fits.

**The attempt's one verdict still decides everything.** Every candidate -- first samples and
repairs alike -- is screened by `/v1/check`, and the first one that elaborates cleanly is linked
and ends the attempt (`verdict.attempt_id` is a primary key). A repair is therefore only ever
asked for with the check outcome that motivated it in hand: the executor sends each screened-out
submission's `CheckOutcome` back into this generator (`protocols.Observation`), which is the whole
reason that channel exists.

Breadth-first by round, as Goedel's pipeline runs: every first sample is checked before any repair
is asked for, and every chain's first repair before any second. A later first sample that proves
the goal would otherwise wait behind two rounds of repairing an earlier one.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from typing import Any

from lean_agent_core.actions import (
    Action,
    Budget,
    Message,
    ObligationContext,
    RequestCompletion,
    SubmitProof,
)
from lean_agent_core.context import DEFAULT_KERNEL_FRACTION
from lean_agent_core.protocols import CheckOutcome, CompletionResponse, Observation, Policy
from lean_agent_core.roles import ModelRole

from lean_agent_policies.feedback import MAX_ERRORS, estimate_tokens, render_errors
from lean_agent_policies.whole_proof import PromptTemplate, WholeProofSampler, extract_lean_block

#: Goedel-Prover-V2's correction turn, verbatim apart from its two placeholders. It lives in the
#: authors' repository rather than the model card; the wording is reproduced because the model
#: was trained to respond to exactly it, and nothing else of that repository is.
GOEDEL_CORRECTION = "repair/goedel_prover_v2_correction.txt"

#: Goedel-Prover-V2's pipeline runs two correction rounds.
DEFAULT_ROUNDS = 2

#: The largest prompt a repair may send, in tokens. Three quarters of a 16k context -- the filter
#: Goedel's own pipeline applies (`max_model_len * 3 / 4`) -- which leaves the rest for the answer.
#: A chain whose next prompt would not fit is not repaired further: the history grows by a whole
#: answer and a page of errors per round, and a prompt past the server's context is a rejected
#: request, not a worse repair.
DEFAULT_PROMPT_BUDGET_TOKENS = 12_288

#: The chat-template markers two new turns add around their content (`<|im_start|>assistant\n`,
#: `<|im_end|>\n`, ...). Small, and counted so the budget is not optimistic by exactly this much.
_TEMPLATE_OVERHEAD_TOKENS = 16


def _expect[T](value: object, kind: type[T]) -> T:
    """What the executor sent back must be what this action produces. A policy that carried on
    with the wrong thing would record an attempt that "tried and failed" when the executor had in
    fact broken its contract."""
    if not isinstance(value, kind):
        raise TypeError(f"expected the executor to send back a {kind.__name__}, got {value!r}")
    return value


@dataclass(frozen=True)
class _Chain:
    """One line of attempts on the goal: a conversation, the answer it ended in, and how the
    kernel judged that answer."""

    label: str
    history: tuple[Message, ...]
    answer: str
    code: str
    line_offset: int
    outcome: CheckOutcome
    #: Exact: the prompt's ids plus this answer's, both as the server reported them.
    tokens: int


@dataclass(frozen=True)
class RepairLoop:
    """Implements `lean_agent_core.protocols.Policy`. Frozen, so `config_hash` cannot drift."""

    sampler: WholeProofSampler = field(default_factory=WholeProofSampler)
    correction: PromptTemplate = field(
        default_factory=lambda: PromptTemplate.load(GOEDEL_CORRECTION)
    )
    rounds: int = DEFAULT_ROUNDS
    max_errors: int = MAX_ERRORS
    prompt_budget_tokens: int = DEFAULT_PROMPT_BUDGET_TOKENS
    #: Band 2's hard share of the prompt (spec §6.6: "Hard 30% [measure]"). The errors' messages
    #: are held to it; the code snippets around them are bounded by construction (at most a dozen
    #: lines each).
    kernel_fraction: float = DEFAULT_KERNEL_FRACTION
    #: A repair asks for one new sample per failed one, as Goedel's pipeline does -- an override on
    #: the deployment's configured sampling, which the opening request uses unchanged.
    repair_sampling: Mapping[str, Any] = field(default_factory=lambda: {"n": 1})

    id: str = "RepairLoop"
    tools: frozenset[str] = frozenset()
    roles: frozenset[ModelRole] = frozenset({ModelRole.PROVER})

    @property
    def prompt_hashes(self) -> dict[str, str]:
        return {**self.sampler.prompt_hashes, "repair": f"sha256:{self.correction.sha256}"}

    @property
    def config_hash(self) -> bytes:
        payload = json.dumps(
            {
                "sampler": self.sampler.config_hash.hex(),
                "correction": self.correction.sha256,
                "rounds": self.rounds,
                "max_errors": self.max_errors,
                "prompt_budget_tokens": self.prompt_budget_tokens,
                "kernel_fraction": self.kernel_fraction,
                "repair_sampling": dict(self.repair_sampling),
            },
            sort_keys=True,
        ).encode()
        return hashlib.sha256(payload).digest()

    def feedback(self, round_: int, chain: _Chain) -> str:
        """The correction turn for `chain`, in correction round `round_` (1-based).

        Goedel numbers the answer being corrected rather than the correction, so the first
        correction says "The proof (Round 0) is not correct" -- reproduced, because the model has
        only ever seen it that way.
        """
        errors = render_errors(
            chain.code,
            chain.line_offset,
            chain.outcome.diagnostics,
            message_budget_tokens=int(self.prompt_budget_tokens * self.kernel_fraction),
            max_errors=self.max_errors,
        )
        return self.correction.render(round=str(round_ - 1), errors=errors)

    async def propose(
        self, ctx: ObligationContext, budget: Budget
    ) -> AsyncGenerator[Action, Observation | None]:
        if budget.attempts_remaining <= 0:
            return

        opening = self.sampler.messages(ctx)
        response = _expect(
            (
                yield RequestCompletion(
                    role=ModelRole.PROVER, messages=opening, sampling=dict(self.sampler.sampling)
                )
            ),
            CompletionResponse,
        )
        chains: list[_Chain] = []
        for index, completion in enumerate(response.completions):
            block = extract_lean_block(completion.text)
            if block is None:
                continue
            header, code, footer = self.sampler.development_parts(ctx, block)
            label = f"sample {index}"
            outcome = _expect(
                (
                    yield SubmitProof(
                        development=header + code + footer,
                        entry=ctx.entry,
                        label=label,
                        timeout_ms=self.sampler.check_timeout_ms,
                    )
                ),
                CheckOutcome,
            )
            chains.append(
                _Chain(
                    label=label,
                    history=opening,
                    answer=completion.text,
                    code=code,
                    line_offset=header.count("\n"),
                    outcome=outcome,
                    tokens=len(response.prompt_token_ids) + len(completion.token_ids),
                )
            )

        for round_ in range(1, self.rounds + 1):
            next_chains: list[_Chain] = []
            for chain in chains:
                feedback = self.feedback(round_, chain)
                projected = chain.tokens + estimate_tokens(feedback) + _TEMPLATE_OVERHEAD_TOKENS
                if projected > self.prompt_budget_tokens:
                    continue
                history = (
                    *chain.history,
                    Message(role="assistant", content=chain.answer),
                    Message(role="user", content=feedback),
                )
                response = _expect(
                    (
                        yield RequestCompletion(
                            role=ModelRole.PROVER,
                            messages=history,
                            sampling=dict(self.repair_sampling),
                        )
                    ),
                    CompletionResponse,
                )
                for index, completion in enumerate(response.completions):
                    block = extract_lean_block(completion.text)
                    if block is None:
                        continue
                    header, code, footer = self.sampler.development_parts(ctx, block)
                    label = f"{chain.label} repair {round_}"
                    if len(response.completions) > 1:
                        label += f".{index}"
                    outcome = _expect(
                        (
                            yield SubmitProof(
                                development=header + code + footer,
                                entry=ctx.entry,
                                label=label,
                                timeout_ms=self.sampler.check_timeout_ms,
                            )
                        ),
                        CheckOutcome,
                    )
                    next_chains.append(
                        _Chain(
                            label=label,
                            history=history,
                            answer=completion.text,
                            code=code,
                            line_offset=header.count("\n"),
                            outcome=outcome,
                            tokens=len(response.prompt_token_ids) + len(completion.token_ids),
                        )
                    )
            chains = next_chains


def _conforms_to_policy_protocol() -> Policy:
    """Structural conformance to `Policy` under `mypy --strict`, as the other policies do."""
    return RepairLoop()
