"""Phase 3's exit criterion, as arithmetic (spec §8).

"The model policy strictly dominates [the symbolic baseline]; reproduce the *relative ranking* of
three open-weights provers on miniF2F; match one published absolute number within a stated
tolerance, with a written account of any gap." And: "Ranking is the load-bearing criterion.
Published absolute numbers depend on unreported harness details, so a gap may indicate nothing;
ranking is robust to that."

Pure logic -- no Postgres, no Lean, no model -- so every judgement the gate makes is testable on
constructed data, and the thresholds are stated here *before* any run produces a number to fit
them to.

**The unit is a problem, and the measure is pass@n.** One `WholeProofSampler` attempt draws *n*
samples and ends at the first that passes the acceptance path, which is exactly how the published
pass@32 figures are defined ("proved by any of 32 samples"). What cannot be recovered from such a
run is how many of the *n* would have passed, so pass@k for k < n is not reported rather than
estimated from a count that was never taken.

**Ranking is a paired comparison.** Every prover faces the same problems, so the uncertainty that
matters is in the per-problem *difference*, not in each pass rate separately -- two provers that
both find the same problems hard move together, and comparing their rates as if independent would
double-count that. The interval is a percentile bootstrap over problems, with a fixed seed so a
report can be re-derived exactly.
"""

from __future__ import annotations

import enum
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any

#: A published pair must be reproduced -- its interval excluding zero in the published direction --
#: only when the published numbers are at least this far apart, in percentage points. Closer than
#: that, 244 problems cannot separate them: a paired difference of a few points has a 95% interval
#: of roughly ±4 points here, so demanding it would make the gate a coin flip. Such pairs are still
#: reported, and must still not be *contradicted*. Stated before any run.
MIN_RESOLVABLE_GAP_POINTS = 5.0

#: The stated tolerance on the one absolute number the gate matches, in percentage points. About
#: one binomial 95% half-width at this sample size (±4.5 for a pass rate near 85% over ~230 scored
#: problems) -- so a gap inside it is indistinguishable from sampling noise, and a gap outside it
#: is a harness difference to account for in writing either way. Stated before any run.
ABSOLUTE_TOLERANCE_POINTS = 5.0

BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 20_260_911


@dataclass(frozen=True)
class ProblemOutcome:
    """One problem's trip through the pipeline under one prover.

    `sealed` and `infra_error` are separate from `proved` for `ProblemResult`'s reason (M2.10): a
    statement that does not seal says something about the corpus, and an infrastructure failure
    says nothing about the problem -- neither may be counted as a prover's miss.
    """

    problem_id: str
    sealed: bool
    proved: bool
    infra_error: bool = False
    #: Samples drawn, and how many of them the token budget cut off (`finish_reason = length`).
    samples: int = 0
    truncated: int = 0
    tokens: int = 0
    kernel_ms: int = 0
    wallclock_ms: int = 0
    #: A screened candidate reached `/v1/link` and was refused. The attempt ends there, so a later
    #: sample that might have linked was never tried -- the one way this measure can undercount.
    link_rejected: bool = False

    @property
    def scored(self) -> bool:
        return self.sealed and not self.infra_error


@dataclass(frozen=True)
class ProverRun:
    prover: str
    samples_per_problem: int
    outcomes: tuple[ProblemOutcome, ...]
    manifest: Mapping[str, Any]

    @property
    def scored(self) -> dict[str, bool]:
        return {o.problem_id: o.proved for o in self.outcomes if o.scored}

    @property
    def solved(self) -> frozenset[str]:
        return frozenset(o.problem_id for o in self.outcomes if o.scored and o.proved)

    @property
    def pass_rate(self) -> float:
        scored = self.scored
        return sum(scored.values()) / len(scored) if scored else 0.0

    def totals(self) -> dict[str, Any]:
        """Spec §7.5's "pass@k with the budget that produced it", with the infra_error rate and --
        new with a 40,960-token window -- how often the budget cut an answer off."""
        outcomes = self.outcomes
        samples = sum(o.samples for o in outcomes)
        return {
            "problems": len(outcomes),
            "scored": len(self.scored),
            "unsealed": sum(1 for o in outcomes if not o.sealed),
            "infra_errors": sum(1 for o in outcomes if o.sealed and o.infra_error),
            "proved": len(self.solved),
            f"pass_at_{self.samples_per_problem}": self.pass_rate,
            "samples": samples,
            "truncated_samples": sum(o.truncated for o in outcomes),
            "truncation_rate": (sum(o.truncated for o in outcomes) / samples) if samples else 0.0,
            "link_rejected": sum(1 for o in outcomes if o.link_rejected),
            "tokens": sum(o.tokens for o in outcomes),
            "kernel_ms": sum(o.kernel_ms for o in outcomes),
            "wallclock_ms": sum(o.wallclock_ms for o in outcomes),
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "prover": self.prover,
            "samples_per_problem": self.samples_per_problem,
            "manifest": dict(self.manifest),
            "totals": self.totals(),
            "outcomes": [o.__dict__ for o in self.outcomes],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ProverRun:
        return cls(
            prover=data["prover"],
            samples_per_problem=int(data["samples_per_problem"]),
            outcomes=tuple(ProblemOutcome(**o) for o in data["outcomes"]),
            manifest=dict(data["manifest"]),
        )


@dataclass(frozen=True)
class Interval:
    """A difference in pass rate with its percentile-bootstrap 95% interval, over `n` problems."""

    point: float
    low: float
    high: float
    n: int


def paired_difference(
    a: Mapping[str, bool],
    b: Mapping[str, bool],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> Interval:
    """`pass(a) - pass(b)` over the problems *both* scored, resampling problems together.

    Only the common problems: a problem one prover could not be scored on (an infrastructure
    failure) says nothing about the other, and keeping it on one side only would compare two
    different problem sets.
    """
    common = sorted(set(a) & set(b))
    if not common:
        raise ValueError("no problem was scored for both provers")
    diffs = [int(a[p]) - int(b[p]) for p in common]
    n = len(diffs)
    rng = random.Random(seed)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(iterations))
    return Interval(
        point=sum(diffs) / n,
        low=means[int(0.025 * iterations)],
        high=means[min(int(0.975 * iterations), iterations - 1)],
        n=n,
    )


class PairVerdict(enum.Enum):
    #: The interval excludes zero in the published direction.
    REPRODUCED = "reproduced"
    #: The estimate is in the published direction; the interval includes zero.
    CONSISTENT = "consistent"
    #: The estimate is against the published direction; the interval includes zero.
    UNRESOLVED = "unresolved"
    #: The interval excludes zero *against* the published direction.
    CONTRADICTED = "contradicted"


@dataclass(frozen=True)
class PairComparison:
    higher: str
    lower: str
    published_gap_points: float
    measured: Interval
    verdict: PairVerdict

    @property
    def required(self) -> bool:
        return self.published_gap_points >= MIN_RESOLVABLE_GAP_POINTS


def classify(interval: Interval) -> PairVerdict:
    """For a difference taken as `published-higher minus published-lower`."""
    if interval.low > 0:
        return PairVerdict.REPRODUCED
    if interval.high < 0:
        return PairVerdict.CONTRADICTED
    return PairVerdict.CONSISTENT if interval.point > 0 else PairVerdict.UNRESOLVED


@dataclass(frozen=True)
class Ranking:
    pairs: tuple[PairComparison, ...]

    @property
    def reproduced(self) -> bool:
        """Spec's load-bearing criterion: no published pair contradicted, and every pair the
        published numbers separate by `MIN_RESOLVABLE_GAP_POINTS` reproduced."""
        return all(p.verdict is not PairVerdict.CONTRADICTED for p in self.pairs) and all(
            p.verdict is PairVerdict.REPRODUCED for p in self.pairs if p.required
        )


def rank(runs: Mapping[str, ProverRun], published: Mapping[str, float]) -> Ranking:
    """Every pair of provers, ordered by their *published* numbers (in percent)."""
    ordered = sorted(runs, key=lambda key: published[key], reverse=True)
    pairs = []
    for higher, lower in combinations(ordered, 2):
        interval = paired_difference(runs[higher].scored, runs[lower].scored)
        pairs.append(
            PairComparison(
                higher=higher,
                lower=lower,
                published_gap_points=published[higher] - published[lower],
                measured=interval,
                verdict=classify(interval),
            )
        )
    return Ranking(pairs=tuple(pairs))


@dataclass(frozen=True)
class AbsoluteComparison:
    measured_points: float
    published_points: float
    tolerance_points: float
    n: int

    @property
    def gap_points(self) -> float:
        return self.measured_points - self.published_points

    @property
    def within_tolerance(self) -> bool:
        return abs(self.gap_points) <= self.tolerance_points


def compare_absolute(
    run: ProverRun,
    published_points: float,
    *,
    tolerance_points: float = ABSOLUTE_TOLERANCE_POINTS,
) -> AbsoluteComparison:
    return AbsoluteComparison(
        measured_points=100.0 * run.pass_rate,
        published_points=published_points,
        tolerance_points=tolerance_points,
        n=len(run.scored),
    )


@dataclass(frozen=True)
class Dominance:
    """ "Strictly dominates", read as sets over the problems both scored: the model proves every
    problem the symbolic portfolio proves, and at least one it does not."""

    missed: frozenset[str]
    gained: frozenset[str]

    @property
    def strict(self) -> bool:
        return not self.missed and bool(self.gained)


def dominance(model: ProverRun, symbolic: ProverRun) -> Dominance:
    common = set(model.scored) & set(symbolic.scored)
    return Dominance(
        missed=frozenset(p for p in common if symbolic.scored[p] and not model.scored[p]),
        gained=frozenset(p for p in common if model.scored[p] and not symbolic.scored[p]),
    )


def sample_problems(ids: Sequence[str], count: int, *, seed: int) -> list[str]:
    """A fixed-seed subset in corpus order, for a pilot: reproducible, and not cherry-picked."""
    if count >= len(ids):
        return list(ids)
    chosen = set(random.Random(seed).sample(list(ids), count))
    return [i for i in ids if i in chosen]
