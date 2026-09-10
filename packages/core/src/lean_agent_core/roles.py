"""`ModelRole` (spec §6.5).

Its own module in `core` rather than a member of `enums.py`, and in `core` rather than in
`packages/models`, for two separate reasons.

Not `enums.py`: that module is specifically "Postgres ENUM types mirrored as Python enums (spec
§5.2)", and `model_role` is not one of them -- no column stores it. Putting it there would make
that docstring false and invite someone to autogenerate a migration for it.

Not `packages/models`: `Policy.roles` is `frozenset[ModelRole]` and `Policy` lives in
`core.protocols`, while `lean_agent_models` already depends on `core`. The enum has to sit on the
side of that dependency both can see.
"""

from __future__ import annotations

from enum import StrEnum


class ModelRole(StrEnum):
    """What a policy asks for. Spec §6.5, verbatim.

    **Policies request a role, never a model.** That indirection is the whole point: "switching
    provers is one TOML line; a four-model ablation is four config files and no code." A policy
    naming a model directly would put the ablation matrix in the source tree.

    `SymbolicPortfolio` declares none of these, which is what makes Phase 2's "zero model calls
    anywhere in the codebase" checkable rather than asserted -- an executor has nothing to ask a
    model for.
    """

    PROVER = "prover"
    DECOMPOSER = "decomposer"
    CRITIC = "critic"
    FORMALIZER = "formalizer"
    INFORMAL = "informal"
