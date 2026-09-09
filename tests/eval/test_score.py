"""M1.9 exit criterion: pass@k with budget accounting, and the infra_error/None exclusion rule
(spec §7.5's scoring discipline) -- pure logic, no external infrastructure needed.
"""

from __future__ import annotations

import pytest
from lean_agent_core.enums import VerdictKind
from lean_agent_eval.score import AttemptOutcome, pass_at_k, score_problem, score_suite


def test_pass_at_k_matches_hand_computed_values() -> None:
    # 1 correct out of 5, drawing 1 sample: probability of hitting the one correct is 1/5.
    assert pass_at_k(n=5, c=1, k=1) == pytest.approx(0.2)
    # 0 correct: no sample can succeed, regardless of k.
    assert pass_at_k(n=5, c=0, k=1) == 0.0
    # n - c < k means every possible k-sample draw includes at least one correct attempt.
    assert pass_at_k(n=5, c=5, k=1) == 1.0
    assert pass_at_k(n=5, c=2, k=4) == 1.0  # n - c = 3 < k = 4


def test_pass_at_k_rejects_k_greater_than_n() -> None:
    with pytest.raises(ValueError, match="undefined"):
        pass_at_k(n=3, c=1, k=4)


def _outcome(
    kind: VerdictKind | None, *, tokens: int = 10, kernel_ms: int = 5, wallclock_ms: int = 100
) -> AttemptOutcome:
    return AttemptOutcome(kind=kind, tokens=tokens, kernel_ms=kernel_ms, wallclock_ms=wallclock_ms)


def test_score_problem_excludes_infra_errors_and_none_from_pass_at_k() -> None:
    outcomes = [
        _outcome(VerdictKind.PROVED),
        _outcome(VerdictKind.ERRORS),
        _outcome(VerdictKind.INFRA_ERROR),
        _outcome(None),
    ]

    result = score_problem(outcomes, ks=(1, 2))

    # n/c exclude the infra_error and None entries -- only the 2 "real" outcomes count.
    assert result.n == 2
    assert result.c == 1
    assert result.infra_errors == 2
    assert result.infra_error_rate == pytest.approx(0.5)
    assert result.pass_at_k[1] == pytest.approx(0.5)
    assert result.pass_at_k[2] == 1.0  # n - c = 1 < k = 2
    # Budget totals every attempt actually spent, infra_errors included -- 4 outcomes, not 2.
    assert result.budget.samples == 4
    assert result.budget.tokens == 40
    assert result.budget.kernel_ms == 20
    assert result.budget.wallclock_ms == 400


def test_score_problem_omits_k_larger_than_sample_count() -> None:
    result = score_problem([_outcome(VerdictKind.PROVED)], ks=(1, 8))
    assert 1 in result.pass_at_k
    assert 8 not in result.pass_at_k


def test_score_problem_with_no_attempts_has_zero_rates_not_a_crash() -> None:
    result = score_problem([], ks=(1,))
    assert result.n == 0
    assert result.infra_error_rate == 0.0
    assert result.pass_at_k == {}


def test_score_suite_averages_per_problem_pass_at_k() -> None:
    outcomes_by_problem = {
        "easy": [_outcome(VerdictKind.PROVED), _outcome(VerdictKind.PROVED)],  # pass@1 = 1.0
        "hard": [_outcome(VerdictKind.ERRORS), _outcome(VerdictKind.ERRORS)],  # pass@1 = 0.0
    }

    result = score_suite(outcomes_by_problem, ks=(1,))

    assert result.mean_pass_at_k[1] == pytest.approx(0.5)
    assert result.total_budget.samples == 4
    assert result.overall_infra_error_rate == 0.0


def test_score_suite_reports_overall_infra_error_rate() -> None:
    outcomes_by_problem = {
        "p1": [_outcome(VerdictKind.PROVED), _outcome(VerdictKind.INFRA_ERROR)],
    }

    result = score_suite(outcomes_by_problem, ks=(1,))

    assert result.overall_infra_error_rate == pytest.approx(0.5)
