"""M1.9 exit criterion: the internal regression suite actually runs against a real
`leankernel serve` process and produces outcomes matching what `check`'s documented scope
predicts -- including the `sorry`-is-not-audited case, which is a real, current limitation being
pinned down, not a bug being tolerated (see suites/internal.py's own docstring).

Local dev / CI: same `lake build` prerequisite as `tests/leanserv/`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from lean_agent_core.enums import VerdictKind
from lean_agent_eval.score import score_suite
from lean_agent_eval.suites.internal import EXPECTED_OK, INTERNAL_SUITE, run_internal_suite


def test_expected_ok_table_covers_every_suite_problem() -> None:
    """A problem added to INTERNAL_SUITE without a matching EXPECTED_OK entry would otherwise
    fail silently (the assertion loop below would just never check it) -- this catches that."""
    assert {p.id for p in INTERNAL_SUITE} == set(EXPECTED_OK)


def test_internal_suite_matches_expected_ok(lake_project_dir: Path) -> None:
    results = asyncio.run(run_internal_suite(lake_project_dir))

    assert set(results) == set(EXPECTED_OK)
    for problem_id, expected_ok in EXPECTED_OK.items():
        actual_kind = results[problem_id].kind
        actual_ok = actual_kind == VerdictKind.PROVED
        assert actual_ok == expected_ok, (
            f"{problem_id}: expected ok={expected_ok}, got kind={actual_kind}"
        )


def test_internal_suite_outcomes_are_scoreable(lake_project_dir: Path) -> None:
    """The suite's own output must actually compose with score.py -- proving the "suite -> run ->
    score" pipeline this milestone is named for genuinely works end to end, not just that each
    piece works in isolation."""
    results = asyncio.run(run_internal_suite(lake_project_dir))
    outcomes_by_problem = {problem_id: [outcome] for problem_id, outcome in results.items()}

    suite_score = score_suite(outcomes_by_problem, ks=(1,))

    assert suite_score.overall_infra_error_rate == 0.0
    # 3 of 4 problems are "ok" per EXPECTED_OK (decide_add, decide_mul_assoc, and the sorry case
    # all elaborate cleanly; only the false statement is rejected) -- so PROVED should be 3/4.
    proved_count = sum(1 for o in results.values() if o.kind == VerdictKind.PROVED)
    assert proved_count == 3
    assert suite_score.total_budget.samples == len(INTERNAL_SUITE)
