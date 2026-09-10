"""What a policy proposes, and what it is given to propose from (spec §6.6, Appendix A).

`Action = SubmitProof | Decompose | CallTool | RequestCompletion | Abandon`, and the rule that
gives the split its point: **the executor performs side effects, not the policy.** A policy that
called leanserv itself could not be replayed from its trajectory, and a tool allowlist it enforced
on itself would not be an allowlist. So every action here is inert data describing something to
do; `executor.py` is the only thing that does any of it.

`ObligationContext` is deliberately band 1 only -- spec §6.6's "sealed goal, pretty-printed, plus
base env", the band that is never evicted. Bands 2-6 (kernel diagnostics, retrieved premises,
proved siblings, error history, reference passages) are `context.py`'s job and exist to feed a
*model*; `SymbolicPortfolio` uses no model and needs none of them. Building the eviction machinery
before anything can consume it would be shaping an interface against no requirement.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from lean_agent_core.roles import ModelRole


@dataclass(frozen=True)
class SubmitProof:
    """Offer `development` as a proof of the sealed goal, entered at `entry`.

    The development never states what it is proving -- it names the sealed constant as its type
    (`def sol : LeanAgent.Goals.G_x := ...`), which is what makes spec §1.1's "the agent never
    writes the goal statement" true at the level of the text a policy actually emits, not just as
    a property the kernel enforces afterwards.

    `label` names which portfolio member or sample produced this, and is recorded in the
    trajectory: an attempt that tried twelve tactics is only useful as training data if you can
    tell which one worked.
    """

    development: str
    entry: str
    label: str
    timeout_ms: int | None = None


@dataclass(frozen=True)
class Decompose:
    """Offer `development` -- a skeleton whose `sorry`s become children -- as a decomposition.

    No executor performs this yet: it needs `/v1/decompose` (M2.1.3, built) *plus* sealing each
    child, inserting its edges, and materializing the bundle they link against, which is M2.6/M2.7.
    Defined here because the `Action` union is spec's, and a union missing a member would push
    every consumer into treating the set as open.
    """

    development: str
    label: str


@dataclass(frozen=True)
class CallTool:
    """Invoke a tool by name. The executor checks the name against `Policy.tools` before doing
    anything, which is the entire reason tool calls are proposed rather than performed."""

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    """One chat turn. `role` here is the *conversational* role (`system`/`user`/`assistant`),
    which has nothing to do with `ModelRole` -- an unfortunate collision inherited from the
    OpenAI wire format, kept because renaming it would diverge from every chat template."""

    role: str
    content: str


@dataclass(frozen=True)
class RequestCompletion:
    """Ask for tokens from a *model* role, never a named model (spec §6.5: "Policies request a
    role, never a model. Switching provers is one TOML line").

    `messages` rather than a single prompt string, because a chat model's prompt is not a string:
    §6.5 requires the chat template be rendered client-side, and a template needs turns to render.
    A bare string could not express a system prompt, which every prover model this system will run
    actually uses -- and §6.6's context bands are assembled into turns, not concatenated into one.
    """

    role: ModelRole
    messages: tuple[Message, ...]
    sampling: dict[str, Any] = field(default_factory=dict)
    #: Overrides the backend's configured seed. `None` leaves the deployment's choice alone, which
    #: for an unseeded sampling policy is what makes each resample genuinely fresh (M3.6).
    seed: int | None = None


@dataclass(frozen=True)
class Abandon:
    """Stop early: this policy has nothing further worth trying on this obligation. Distinct from
    running out of proposals, which is an ordinary exhausted attempt -- `Abandon` says the policy
    knows more attempts of this kind will not help."""

    reason: str


Action = SubmitProof | Decompose | CallTool | RequestCompletion | Abandon


@dataclass(frozen=True)
class ObligationContext:
    """Spec §6.6's band 1: the sealed goal and its environment. Never evicted, because without it
    there is nothing to prove.

    `goal_decl` is the sealed constant's fully-qualified name and `goal_src` its statement as
    text. Both are present and they are not redundant: a policy proves the goal by *naming*
    `goal_decl` (so it never restates the statement), while `goal_src` is what a model would read
    and what a human reads in a log. A policy that used `goal_src` to build a development would be
    writing the statement, which is the thing the design forbids.
    """

    obligation_id: uuid.UUID
    run_id: uuid.UUID
    base_env_digest: str
    bundle_sha: str
    goal_decl: str
    goal_src: str
    entry: str
    level_params: tuple[str, ...] = ()
    #: The run's `allow_sorry`, carried here so a policy executor's screen can agree with what
    #: `/v1/link` will actually enforce. Without it the screen has to guess, and a screen that
    #: disagrees with the enforcement point is the drift this codebase avoids everywhere else
    #: (the state machine and the privilege model being two views of one guarantee). Defaults to
    #: the column's own default, so a caller that does not set it gets the stricter behaviour.
    allow_sorry: bool = False


@dataclass(frozen=True)
class Budget:
    """What is left to spend on this obligation (spec §6.6's `propose(ctx, budget)`).

    Advisory to the policy and enforced elsewhere: `attempts_remaining` is decided by the state
    machine, tokens and kernel time by whatever meters them. A policy consults it to decide how
    hard to try -- a twelve-tactic portfolio with 200 ms of kernel budget left should cut itself
    short rather than start.
    """

    attempts_remaining: int
    tokens_remaining: int | None = None
    kernel_ms_remaining: int | None = None
