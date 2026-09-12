"""`WholeProofSampler` (spec §6.6): sample *n* whole proofs from the prover role, check them all.

The first policy that asks a model for anything. Its shape is spec's one line -- sample, then check
every sample -- and everything interesting is in the two places a model's text meets this system:
what the model is shown, and what is done with what it writes.

**What the model is shown** is band 1 and nothing else: the sealed goal, pretty-printed, plus the
base environment (spec §6.6). Rendered in the prover's *trained* format rather than in
`ContextBuilder`'s headings, because a whole-proof prover is a model of one prompt shape --
Goedel-Prover-V2's is "Complete the following Lean 4 code" around a fenced file ending in
`:= by sorry` -- and a prompt it was never trained on is a different, worse model. The prompt text
is a versioned asset under `prompts/`, hashed into `config_hash` and `prompt_hashes` (spec §6.6:
"Prompts under `packages/policies/prompts/` are versioned assets hashed into the manifest").

The statement shown is the *sealed* one -- `∀ (a b : ℝ), a * b = 180 → ...` -- not the signature
the problem was submitted with, because the sealed statement is the only one an obligation has.
Measured against Goedel-Prover-V2-8B: it takes this in its stride, opening every sample with
`intro a b h₁ h₂` and restating the theorem verbatim.

**What is done with what the model writes** is where §1.1 is kept. The model restates a theorem;
this policy never lets that restatement be what is proved. The model's final code block goes into
the development as an *auxiliary* theorem, and the entry is always

    def sol_n : LeanAgent.Goals.G_n := by unfold LeanAgent.Goals.G_n; exact LeanAgent.Sol.G_n

-- the entry's type is the sealed constant, exactly as `SymbolicPortfolio` emits it, and the kernel
decides at link time whether the model's theorem inhabits it. A model that silently weakened the
statement produces an `exact` that does not typecheck, which is a failed candidate, never a wrong
proof. That is also why the model's text needs no surgery: pulling a tactic block out of
`theorem ... := by <here>` would mean parsing Lean by hand (indentation, `:= by` vs a term, helper
lemmas declared ahead of the theorem -- which Goedel does write), where including the whole block
leaves Lean to parse Lean.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
import string
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lean_agent_core.actions import (
    Action,
    Budget,
    Message,
    ObligationContext,
    RequestCompletion,
    SubmitProof,
)
from lean_agent_core.protocols import CompletionResponse, Observation, Policy
from lean_agent_core.roles import ModelRole

PROMPTS = Path(__file__).parent / "prompts"

#: Goedel-Prover-V2's own prompt, verbatim from its model card and `src/utils.py`
#: (Goedel-LM/Goedel-Prover-V2). Verbatim matters: spec §6.6 says to "port them verbatim and treat
#: divergence as a measured change", and a prover is only as good as the prompt shape it was
#: trained on.
GOEDEL_PROVER_V2 = "whole_proof/goedel_prover_v2.txt"

#: Kimina-Prover's prompt and system turn, verbatim from its model card's vLLM quick start
#: (AI-MO/Kimina-Prover-Distill-8B): the problem in natural language under "# Problem:", then the
#: formal statement ending at `:= by`.
KIMINA_PROVER = "whole_proof/kimina_prover.txt"
KIMINA_PROVER_SYSTEM = "whole_proof/kimina_prover_system.txt"

#: The `open` line Goedel-Prover-V2's training header carries, so the model's proofs lean on it:
#: its final code block restates the theorem but *not* the header (measured), which means names
#: like `sq_nonneg` or `Real.sqrt_nonneg` resolve only because this line is in force. It is shown
#: in the prompt and repeated in the development, so the file that is checked is the file the
#: model was told it was writing.
GOEDEL_OPENS = "open BigOperators Real Nat Topology Rat"

#: Bounded, and deliberately not upstream's `maxHeartbeats 0`. Spec §7.2 denies `maxHeartbeats 0`
#: outright, and M2.10 found the concrete cost: an unbounded tactic runs until the *wallclock*
#: timeout, which SIGKILLs a ~6 GiB full-Mathlib worker and pays ~30 s to re-warm it. Every
#: published prover prompt shows `0` -- Goedel-Prover-V2's card and pipeline header, Pythagoras's
#: and Kimina's cards (M3.9's note said the card showed 400000; it does not) -- so the prompt here
#: differs from theirs in that one number, a stated divergence: it shows the value the development
#: actually sets. 400000 is double Lean's default, since a prover's proofs are heavier than a
#: single portfolio tactic.
DEFAULT_MAX_HEARTBEATS = 400_000

#: Per-candidate `/v1/check` wallclock. Generous next to the portfolio's 10 s because a whole proof
#: is a chain of tactics, and bounded so a pathological sample cannot hold the worker indefinitely.
DEFAULT_CHECK_TIMEOUT_MS = 60_000

#: A fenced Lean block. Goedel's own extractor is `r'```lean4\n(.*?)\n```'`; `lean` is accepted
#: too because other provers fence with it, and nothing about a block's contents differs.
_FENCE = re.compile(r"```lean4?\n(.*?)\n```", re.DOTALL)


@dataclass(frozen=True)
class PromptTemplate:
    """One versioned prompt asset: its name, its exact text, and the digest the manifest records."""

    name: str
    text: str

    @classmethod
    def load(cls, name: str) -> PromptTemplate:
        return cls(name=name, text=(PROMPTS / name).read_text(encoding="utf-8"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def render(self, **fields: str) -> str:
        """`string.Template`, not `str.format`: a prompt about Lean is full of `{` and `}`, and a
        `format` template would make every binder in an edited prompt a placeholder."""
        return string.Template(self.text).substitute(fields)


class StatementLayout(enum.Enum):
    """How the formal statement sits inside a prover's prompt, copied from the code or card that
    produced each prover's published numbers (M3.12) -- the shape a model was trained on is part of
    the model, down to where a fence closes. Each is held byte-for-byte to its source by a test."""

    #: Goedel-Prover-V2's released pipeline (`src/utils.py`, `prover_inference`): DeepSeek-Prover's
    #: miniF2F header, the informal docstring, then `code.split(":= by")[0] + ":= by sorry"` -- so
    #: the fence closes straight after `sorry`.
    GOEDEL_PIPELINE = "goedel_pipeline"
    #: Pythagoras-Prover's card: the same file with `:= by` and an indented `sorry` on its own line,
    #: `.strip()`ped before formatting.
    PYTHAGORAS_CARD = "pythagoras_card"
    #: Kimina-Prover's card: header lines with no blank line between them, ending at `:= by`.
    KIMINA_CARD = "kimina_card"
    #: What M3.9 shipped and M3.9/M3.10 recorded: the card's file *with* a trailing newline, which
    #: no published prompt has, and no docstring. Kept so those recordings keep replaying.
    M3_9 = "m3.9"


@dataclass(frozen=True)
class ProverFormat:
    """One prover's published prompt: its user template, its system turn if it has one, and the
    layout of the statement inside it."""

    prompt: PromptTemplate
    layout: StatementLayout
    system: PromptTemplate | None = None

    @classmethod
    def goedel_pipeline(cls) -> ProverFormat:
        return cls(PromptTemplate.load(GOEDEL_PROVER_V2), StatementLayout.GOEDEL_PIPELINE)

    @classmethod
    def pythagoras_card(cls) -> ProverFormat:
        """Pythagoras-Prover's card prompt is Goedel's text, word for word, around its own layout."""
        return cls(PromptTemplate.load(GOEDEL_PROVER_V2), StatementLayout.PYTHAGORAS_CARD)

    @classmethod
    def kimina_card(cls) -> ProverFormat:
        return cls(
            PromptTemplate.load(KIMINA_PROVER),
            StatementLayout.KIMINA_CARD,
            PromptTemplate.load(KIMINA_PROVER_SYSTEM),
        )

    @classmethod
    def m3_9(cls) -> ProverFormat:
        return cls(PromptTemplate.load(GOEDEL_PROVER_V2), StatementLayout.M3_9)


def extract_lean_block(text: str) -> str | None:
    """The model's final Lean code block, or `None` if it did not finish writing one.

    **The last block, never the first.** Measured: every Goedel-Prover-V2 sample writes two, and the
    first is a proof *sketch* -- the theorem with `have ... := by sorry` steps -- before the plan's
    explanation and a "Complete Lean 4 Proof" block. Taking the first would submit the sketch,
    which elaborates (a `sorry` is only a warning) and is exactly what the executor's `sorryAx`
    screen exists to catch; taking the last submits the proof.

    A reasoning model's `<think>` section may contain code blocks that are drafts, not answers, so
    only text after the final `</think>` is searched -- and an unclosed `<think>` means the sample
    ran out of tokens mid-thought and has no answer at all. Goedel-Prover-V2-8B emits no think
    section (measured); this is for the provers that do.

    This is parsing a *model's prose* for a markdown fence, not analysing Lean source, which is why
    a regex is acceptable here when it is not for decomposition or sorry-finding: nothing about the
    block's meaning is inferred from its text. Lean decides that, at `/v1/check`.
    """
    if "<think>" in text and "</think>" not in text:
        return None
    answer = text.rsplit("</think>", 1)[-1]
    blocks = _FENCE.findall(answer)
    return str(blocks[-1]) if blocks else None


def _without_imports(block: str) -> str:
    """Drop the block's own `import` lines. The base environment is the worker's, fixed before the
    model ran, and an `import` anywhere but a file's head is a parse error -- so a model that
    restates `import Mathlib` would otherwise fail on a proof that is fine."""
    return "\n".join(line for line in block.split("\n") if not line.startswith("import "))


@dataclass(frozen=True)
class WholeProofSampler:
    """Implements `lean_agent_core.protocols.Policy`. Frozen, so `config_hash` cannot drift from
    the configuration it was computed for."""

    #: Goedel-Prover-V2's released pipeline by default (M3.12; M3.9 shipped a layout no published
    #: prompt has, which `ProverFormat.m3_9` keeps for the recordings made with it).
    prompt_format: ProverFormat = field(default_factory=ProverFormat.goedel_pipeline)
    opens: str = GOEDEL_OPENS
    max_heartbeats: int = DEFAULT_MAX_HEARTBEATS
    check_timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS
    #: Overrides on the deployment's configured sampling, empty by default. Configuration is the
    #: default and a policy narrows it (M3.7): spec §6.5's "sample n at temperature t" takes n and
    #: t from `[models.prover].sampling`, so an ablation is a config file rather than a code change.
    sampling: Mapping[str, Any] = field(default_factory=dict)

    id: str = "WholeProofSampler"
    tools: frozenset[str] = frozenset()
    roles: frozenset[ModelRole] = frozenset({ModelRole.PROVER})

    @property
    def prompt_hashes(self) -> dict[str, str]:
        """Spec §7.3's `policies[].prompt_hashes`, in the manifest's own `sha256:` form."""
        hashes = {"prover": f"sha256:{self.prompt_format.prompt.sha256}"}
        if self.prompt_format.system is not None:
            hashes["prover_system"] = f"sha256:{self.prompt_format.system.sha256}"
        return hashes

    @property
    def config_hash(self) -> bytes:
        """Everything that changes what is asked or what is checked: the prompt's *content* (not
        its file name -- an edited prompt under the same name is a different experiment), its
        system turn and statement layout, the header lines repeated into every development, the
        check budget, and any sampling override."""
        system = self.prompt_format.system
        payload = json.dumps(
            {
                "prompt": self.prompt_format.prompt.sha256,
                "system": system.sha256 if system is not None else None,
                "layout": self.prompt_format.layout.value,
                "opens": self.opens,
                "max_heartbeats": self.max_heartbeats,
                "check_timeout_ms": self.check_timeout_ms,
                "sampling": dict(self.sampling),
            },
            sort_keys=True,
        ).encode()
        return hashlib.sha256(payload).digest()

    @staticmethod
    def _names(ctx: ObligationContext) -> tuple[str, str, str]:
        """`(universes, statement name, entry namespace)`. The statement is shown under the sealed
        goal's own short name (`G_7`), which cannot collide with the entry (`sol_7`)."""
        universes = "" if not ctx.level_params else ".{" + ", ".join(ctx.level_params) + "}"
        return universes, ctx.goal_decl.rsplit(".", 1)[-1], ctx.entry.rsplit(".", 1)[0]

    def formal_statement(self, ctx: ObligationContext) -> str:
        """Band 1, as a Lean file the prover recognises: the base env's imports, the header, the
        problem's informal statement as a docstring when there is one, and the sealed goal as a
        theorem to complete -- laid out as the prover's own published code or card lays it out.

        The docstring is `/-- {text}-/`, with no space before the close: DeepSeek-Prover's miniF2F
        (what Goedel-Prover-V2's pipeline reads) and both cards write it that way.
        """
        universes, name, _ = self._names(ctx)
        imports = "".join(f"import {module}\n" for module in ctx.base_env_imports)
        heartbeats = f"set_option maxHeartbeats {self.max_heartbeats}"
        theorem = f"theorem {name}{universes} : {ctx.goal_src} := by"
        doc = f"/-- {ctx.informal_statement}-/\n" if ctx.informal_statement else ""
        layout = self.prompt_format.layout
        if layout is StatementLayout.KIMINA_CARD:
            return f"{imports}{heartbeats}\n{self.opens}\n{doc}{theorem}\n"
        header = f"{imports}\n{heartbeats}\n\n{self.opens}\n\n"
        if layout is StatementLayout.GOEDEL_PIPELINE:
            return f"{header}{doc}{theorem} sorry"
        if layout is StatementLayout.PYTHAGORAS_CARD:
            return f"{header}{doc}{theorem}\n  sorry"
        return f"{header}{theorem}\n  sorry\n"

    def messages(self, ctx: ObligationContext) -> tuple[Message, ...]:
        """The system turn, if the prover's format has one, then the prompt. Kimina's `# Problem:`
        is left empty when no informal statement was supplied -- the card's format, with nothing
        invented to fill it."""
        fmt = self.prompt_format
        user = Message(
            role="user",
            content=fmt.prompt.render(
                formal_statement=self.formal_statement(ctx),
                informal_statement=ctx.informal_statement or "",
            ),
        )
        if fmt.system is None:
            return (user,)
        return (Message(role="system", content=fmt.system.text), user)

    def development(self, ctx: ObligationContext, block: str) -> str:
        """The model's block as an auxiliary theorem, and an entry typed by the sealed constant.

        See the module docstring for why the model's theorem is included whole rather than having
        its tactic block cut out. The header (`maxHeartbeats`, `opens`) is the one the prompt
        showed, repeated here because the model's final block leaves it out.
        """
        return "".join(self.development_parts(ctx, block))

    def development_parts(self, ctx: ObligationContext, block: str) -> tuple[str, str, str]:
        """`development` as `(header, the model's code, footer)`, concatenated verbatim.

        Split out for `RepairLoop` (M3.10), which has to show a model *where in its own code* each
        error is. Diagnostics are positioned in the development, and the model's code starts after
        the header -- so `header.count("\\n")` is the exact line offset between the two, and a
        position outside the code (in the footer's entry) is one the model never wrote.
        """
        universes, name, namespace = self._names(ctx)
        goal = f"{ctx.goal_decl}{universes}"
        entry = ctx.entry.rsplit(".", 1)[-1]
        header = (
            "set_option linter.defProp false\n"
            f"set_option maxHeartbeats {self.max_heartbeats}\n"
            f"{self.opens}\n"
            f"namespace {namespace}\n\n"
        )
        footer = (
            "\n\n"
            f"def {entry}{universes} : {goal} := by unfold {ctx.goal_decl}; "
            f"exact {namespace}.{name}{universes}\n"
            f"end {namespace}"
        )
        return header, _without_imports(block), footer

    async def propose(
        self, ctx: ObligationContext, budget: Budget
    ) -> AsyncGenerator[Action, Observation | None]:
        """One request for *n* samples, then one submission per sample that wrote a proof.

        One request rather than *n*: a server samples `n` completions against one prefill, and
        the response cache stores all of one request's samples as one entry (M3.6) -- *n* separate
        requests would pay the prompt *n* times and cache *n* unrelated rows.

        A sample with no final code block is not submitted, and is not lost either: every
        sample's tokens are on the trajectory (M3.7), and the executor's step records how each
        one ended, so "four samples, one submission" reads as three that ran out of tokens.
        """
        if budget.attempts_remaining <= 0:
            return
        response = yield RequestCompletion(
            role=ModelRole.PROVER, messages=self.messages(ctx), sampling=dict(self.sampling)
        )
        if not isinstance(response, CompletionResponse):
            raise TypeError(
                f"expected the executor to send back a CompletionResponse, got {response!r}"
            )
        for index, completion in enumerate(response.completions):
            block = extract_lean_block(completion.text)
            if block is None:
                continue
            yield SubmitProof(
                development=self.development(ctx, block),
                entry=ctx.entry,
                label=f"sample {index}",
                timeout_ms=self.check_timeout_ms,
            )


def _conforms_to_policy_protocol() -> Policy:
    """Structural conformance to `Policy` under `mypy --strict`, as `SymbolicPortfolio` does."""
    return WholeProofSampler()
