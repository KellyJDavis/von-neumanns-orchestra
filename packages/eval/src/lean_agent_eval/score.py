"""pass@k scoring with budget accounting (spec §7.5's "scoring discipline": "report pass@k with
the budget that produced it (tokens, kernel-seconds, wallclock, sample count); report the
infra_error rate alongside").

`AttemptOutcome` is a minimal, scoring-only slice of `attempt`/`verdict` (spec §5.3) -- not the
full ORM/Pydantic models, which carry many fields (lease bookkeeping, policy config hashes, ...)
scoring has no use for. Nothing in this module touches Postgres; a real caller (once Phase 2's
control loop exists to produce real attempts) is responsible for reading rows and mapping them to
`AttemptOutcome` before calling in here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import comb

from lean_agent_core.enums import VerdictKind


@dataclass(frozen=True)
class AttemptOutcome:
    """One attempt's outcome and the four budget dimensions spec's scoring discipline names.
    `kind=None` means the attempt has no verdict at all yet (e.g. it crashed before `leanserv`
    could classify it, or is still in flight) -- treated the same as `INFRA_ERROR` for scoring
    purposes: not informative about whether the content is provable, and excluded from pass@k's
    own n/c count for exactly that reason.
    """

    kind: VerdictKind | None
    tokens: int
    kernel_ms: int
    wallclock_ms: int


@dataclass(frozen=True)
class BudgetTotals:
    tokens: int
    kernel_ms: int
    wallclock_ms: int
    samples: int


@dataclass(frozen=True)
class ProblemScore:
    """Score for one obligation's attempts. `pass_at_k` only has entries for `k <= n` (a k larger
    than the sample count actually drawn is undefined, not zero or one, so it's omitted rather
    than guessed at). `budget` totals every attempt actually spent, `infra_error`s included --
    those attempts cost real tokens/wallclock even though they don't count toward `n`/`c`.
    """

    n: int
    c: int
    infra_errors: int
    infra_error_rate: float
    pass_at_k: dict[int, float]
    budget: BudgetTotals


def pass_at_k(n: int, c: int, k: int) -> float:
    """The unbiased pass@k estimator (Chen et al. 2021, "Evaluating Large Language Models Trained
    on Code"): the probability that at least one of `k` samples drawn *without replacement* from a
    pool of `n` attempts (`c` of them correct) succeeds. Computed as `1 - C(n-c, k) / C(n, k)`
    rather than by actually drawing samples, which is both exact and doesn't vary run to run for
    the same `n`/`c`/`k` -- the whole reason this estimator exists over a naive Monte Carlo one.

    Raises `ValueError` for `k > n` (undefined: there aren't `k` samples to draw from) rather than
    returning a guessed value -- `ProblemScore.pass_at_k` filters `k`s down to `k <= n` before
    calling this, precisely to never hit that case from `score_problem`.
    """
    if k > n:
        raise ValueError(f"pass@{k} is undefined for n={n} samples")
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def score_problem(outcomes: Sequence[AttemptOutcome], ks: Sequence[int]) -> ProblemScore:
    """Score every attempt made on one obligation. `ks` is typically `(1, k, ...)` for whatever
    sample counts the caller cares about (e.g. `pass@1` and `pass@8`); each entry larger than the
    actual sample count is silently omitted from the result rather than raising, since a caller
    scoring several problems with the same fixed `ks` list shouldn't need to special-case problems
    with fewer attempts than the largest `k` requested.
    """
    total_attempts = len(outcomes)
    real_outcomes = [
        o for o in outcomes if o.kind is not None and o.kind != VerdictKind.INFRA_ERROR
    ]
    n = len(real_outcomes)
    c = sum(1 for o in real_outcomes if o.kind == VerdictKind.PROVED)
    infra_errors = total_attempts - n
    infra_error_rate = infra_errors / total_attempts if total_attempts else 0.0

    budget = BudgetTotals(
        tokens=sum(o.tokens for o in outcomes),
        kernel_ms=sum(o.kernel_ms for o in outcomes),
        wallclock_ms=sum(o.wallclock_ms for o in outcomes),
        samples=total_attempts,
    )
    pass_at_k_by_k = {k: pass_at_k(n, c, k) for k in ks if k <= n}
    return ProblemScore(
        n=n,
        c=c,
        infra_errors=infra_errors,
        infra_error_rate=infra_error_rate,
        pass_at_k=pass_at_k_by_k,
        budget=budget,
    )


@dataclass(frozen=True)
class SuiteScore:
    """Aggregate over every problem in a suite. `mean_pass_at_k` averages each problem's own
    `pass_at_k[k]` (only over problems that actually have an entry for that `k`) rather than
    pooling all problems' `n`/`c` into one `pass_at_k` call -- per-problem pass@k, then averaged,
    is the standard reporting convention (miniF2F/PutnamBench-style leaderboards report it this
    way), and pooling would let a handful of heavily-resampled easy problems dominate the score.
    """

    problems: dict[str, ProblemScore]
    mean_pass_at_k: dict[int, float]
    total_budget: BudgetTotals
    overall_infra_error_rate: float


def score_suite(
    outcomes_by_problem: Mapping[str, Sequence[AttemptOutcome]], ks: Sequence[int]
) -> SuiteScore:
    problems = {
        problem_id: score_problem(outcomes, ks)
        for problem_id, outcomes in outcomes_by_problem.items()
    }

    mean_pass_at_k: dict[int, float] = {}
    for k in ks:
        applicable = [p.pass_at_k[k] for p in problems.values() if k in p.pass_at_k]
        if applicable:
            mean_pass_at_k[k] = sum(applicable) / len(applicable)

    total_budget = BudgetTotals(
        tokens=sum(p.budget.tokens for p in problems.values()),
        kernel_ms=sum(p.budget.kernel_ms for p in problems.values()),
        wallclock_ms=sum(p.budget.wallclock_ms for p in problems.values()),
        samples=sum(p.budget.samples for p in problems.values()),
    )
    total_attempts = total_budget.samples
    total_infra_errors = sum(p.infra_errors for p in problems.values())
    overall_infra_error_rate = total_infra_errors / total_attempts if total_attempts else 0.0

    return SuiteScore(
        problems=problems,
        mean_pass_at_k=mean_pass_at_k,
        total_budget=total_budget,
        overall_infra_error_rate=overall_infra_error_rate,
    )
