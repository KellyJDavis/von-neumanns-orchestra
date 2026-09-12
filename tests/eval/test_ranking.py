"""M3.12 -- the exit gate's arithmetic, on constructed data.

No infrastructure. Every judgement the gate makes -- reproduced, contradicted, within tolerance,
dominates -- is a function of per-problem outcomes, so each is pinned here on inputs whose right
answer is known, before any real run produces a number the thresholds could be fitted to.
"""

from __future__ import annotations

import pytest
from lean_agent_eval.ranking import (
    ABSOLUTE_TOLERANCE_POINTS,
    MIN_RESOLVABLE_GAP_POINTS,
    Interval,
    PairVerdict,
    ProblemOutcome,
    ProverRun,
    classify,
    compare_absolute,
    dominance,
    paired_difference,
    rank,
    sample_problems,
)


def run(prover: str, proved: dict[str, bool], **extra: ProblemOutcome) -> ProverRun:
    outcomes = [ProblemOutcome(problem_id=p, sealed=True, proved=ok) for p, ok in proved.items()]
    outcomes += list(extra.values())
    return ProverRun(prover=prover, samples_per_problem=4, outcomes=tuple(outcomes), manifest={})


IDS = [f"p{i:03d}" for i in range(200)]


def test_an_unsealed_or_infra_problem_is_never_a_miss() -> None:
    """M2.10's distinction, kept: neither says anything about the prover."""
    r = run(
        "x",
        {"a": True, "b": False},
        unsealed=ProblemOutcome(problem_id="c", sealed=False, proved=False),
        infra=ProblemOutcome(problem_id="d", sealed=True, proved=False, infra_error=True),
    )
    assert r.scored == {"a": True, "b": False}
    assert r.pass_rate == 0.5
    totals = r.totals()
    assert (totals["unsealed"], totals["infra_errors"], totals["scored"]) == (1, 1, 2)


def test_the_difference_is_paired_and_only_over_problems_both_scored() -> None:
    a = {"p1": True, "p2": True, "p3": False, "only_a": True}
    b = {"p1": True, "p2": False, "p3": False, "only_b": False}
    interval = paired_difference(a, b, iterations=2000)
    assert interval.n == 3
    assert interval.point == pytest.approx(1 / 3)
    assert interval.low <= interval.point <= interval.high


def test_the_bootstrap_is_reproducible() -> None:
    """A report must be re-derivable exactly from its outcomes, so the seed is fixed."""
    a = {p: i % 3 != 0 for i, p in enumerate(IDS)}
    b = {p: i % 4 != 0 for i, p in enumerate(IDS)}
    assert paired_difference(a, b) == paired_difference(a, b)


def test_a_clear_gap_is_reproduced_and_a_reversed_one_is_contradicted() -> None:
    strong = {p: i % 10 != 0 for i, p in enumerate(IDS)}  # 90%
    weak = {p: i % 10 not in (0, 1, 2) for i, p in enumerate(IDS)}  # 70%, a subset of strong
    assert classify(paired_difference(strong, weak)) is PairVerdict.REPRODUCED
    assert classify(paired_difference(weak, strong)) is PairVerdict.CONTRADICTED


def test_an_interval_straddling_zero_is_consistent_or_unresolved_by_its_sign() -> None:
    assert classify(Interval(point=0.01, low=-0.02, high=0.04, n=200)) is PairVerdict.CONSISTENT
    assert classify(Interval(point=-0.01, low=-0.04, high=0.02, n=200)) is PairVerdict.UNRESOLVED
    assert classify(Interval(point=0.0, low=-0.02, high=0.02, n=200)) is PairVerdict.UNRESOLVED


def test_the_ranking_requires_only_the_pairs_the_published_numbers_separate() -> None:
    """Goedel and Pythagoras are 1.5 points apart as published -- inside what 244 problems can
    resolve -- so a tie between them passes; Kimina, 6.7 and 8.2 points below them, must come out
    significantly last."""
    pythagoras = run("pythagoras", {p: i % 10 != 0 for i, p in enumerate(IDS)})
    goedel = run("goedel", {p: i % 10 != 0 for i, p in enumerate(IDS)})
    kimina = run("kimina", {p: i % 10 not in (0, 1, 2) for i, p in enumerate(IDS)})
    published = {"pythagoras": 86.07, "goedel": 84.6, "kimina": 77.86}

    ranking = rank({"goedel": goedel, "kimina": kimina, "pythagoras": pythagoras}, published)
    by_pair = {(p.higher, p.lower): p for p in ranking.pairs}
    assert set(by_pair) == {
        ("pythagoras", "goedel"),
        ("pythagoras", "kimina"),
        ("goedel", "kimina"),
    }
    assert not by_pair[("pythagoras", "goedel")].required
    assert by_pair[("pythagoras", "goedel")].verdict is PairVerdict.UNRESOLVED
    assert by_pair[("goedel", "kimina")].required
    assert by_pair[("goedel", "kimina")].verdict is PairVerdict.REPRODUCED
    assert ranking.reproduced


def test_the_ranking_fails_when_a_required_pair_is_only_consistent() -> None:
    near = {p: i % 10 != 0 for i, p in enumerate(IDS)}
    nearly = {p: (i % 10 != 0) and i != 7 for i, p in enumerate(IDS)}  # one problem worse
    ranking = rank(
        {"goedel": run("goedel", near), "kimina": run("kimina", nearly)},
        {"goedel": 84.6, "kimina": 77.86},
    )
    (pair,) = ranking.pairs
    assert pair.verdict is PairVerdict.CONSISTENT
    assert not ranking.reproduced


def test_the_ranking_fails_on_any_contradicted_pair_required_or_not() -> None:
    better = {p: i % 10 != 0 for i, p in enumerate(IDS)}
    worse = {p: i % 10 not in (0, 1, 2) for i, p in enumerate(IDS)}
    ranking = rank(
        {"a": run("a", worse), "b": run("b", better)}, {"a": 86.0, "b": 85.0}
    )  # published a > b by 1 point -- not required -- but measured significantly reversed
    (pair,) = ranking.pairs
    assert not pair.required and pair.verdict is PairVerdict.CONTRADICTED
    assert not ranking.reproduced


def test_the_absolute_match_uses_the_stated_tolerance() -> None:
    eighty = run("goedel", {p: i % 5 != 0 for i, p in enumerate(IDS)})  # 80%
    close = compare_absolute(eighty, 84.6)
    assert close.gap_points == pytest.approx(-4.6)
    assert close.within_tolerance and close.tolerance_points == ABSOLUTE_TOLERANCE_POINTS
    assert not compare_absolute(eighty, 86.0).within_tolerance


def test_dominance_needs_every_symbolic_proof_and_one_more() -> None:
    symbolic = run("symbolic", {"easy": True, "hard": False, "harder": False})
    assert dominance(run("m", {"easy": True, "hard": True, "harder": False}), symbolic).strict
    missing = dominance(run("m", {"easy": False, "hard": True, "harder": True}), symbolic)
    assert missing.missed == frozenset({"easy"}) and not missing.strict
    assert not dominance(run("m", {"easy": True, "hard": False, "harder": False}), symbolic).strict


def test_a_run_round_trips_through_its_report() -> None:
    original = run(
        "goedel",
        {"a": True},
        cut=ProblemOutcome(problem_id="b", sealed=True, proved=False, samples=4, truncated=2),
    )
    assert ProverRun.from_json(original.to_json()) == original
    assert original.totals()["truncation_rate"] == 0.5


def test_a_pilot_subset_is_fixed_by_its_seed_and_keeps_corpus_order() -> None:
    first = sample_problems(IDS, 10, seed=7)
    assert first == sample_problems(IDS, 10, seed=7)
    assert first == sorted(first) and len(first) == 10
    assert sample_problems(IDS, 500, seed=7) == IDS


def test_the_thresholds_were_stated_before_any_run() -> None:
    """Pinned so that moving one is a visible change to a stated criterion, not a quiet refit."""
    assert (MIN_RESOLVABLE_GAP_POINTS, ABSOLUTE_TOLERANCE_POINTS) == (5.0, 5.0)
