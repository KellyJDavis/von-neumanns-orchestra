"""`lean_agent_eval.baseline`'s comparison logic, with no infrastructure.

`tests/eval/test_minif2f.py` establishes that the baseline is recorded from a real run and that it
genuinely fails on drift; that test costs two minutes and a full-Mathlib worker. These cover the
branches of `compare` that a real run cannot reach on demand -- a problem vanishing, an axiom cone
moving, the policy being reconfigured -- and they run in milliseconds.
"""

from __future__ import annotations

import json

import pytest
from lean_agent_eval.baseline import (
    Phase2Baseline,
    ProblemBaseline,
    compare,
    load,
    normalize_artifact,
    save,
    sha256_text,
)


def _problem(pid: str = "p1", **overrides: object) -> ProblemBaseline:
    fields: dict[str, object] = {
        "id": pid,
        "sealed": True,
        "proved": True,
        "tactic": "omega",
        "goal_src_sha256": sha256_text(f"goal-{pid}"),
        "proof_sha256": sha256_text(f"proof-{pid}"),
        "axioms": ("propext",),
    }
    fields.update(overrides)
    return ProblemBaseline(**fields)  # type: ignore[arg-type]


def _baseline(*problems: ProblemBaseline, **overrides: object) -> Phase2Baseline:
    fields: dict[str, object] = {
        "corpus_sha256": "c" * 64,
        "policy_id": "SymbolicPortfolio",
        "policy_config_hash": "a" * 64,
        "tactics": ("rfl", "omega"),
        "problems": problems or (_problem(),),
        "artifact_sha256": "f" * 64,
        "artifact_holes": 1,
    }
    fields.update(overrides)
    return Phase2Baseline(**fields)  # type: ignore[arg-type]


def test_an_identical_run_reports_no_differences() -> None:
    assert compare(_baseline(), _baseline()) == ()


def test_a_changed_proof_is_reported_even_when_the_outcome_is_unchanged() -> None:
    """The drift Phase 3 is most likely to introduce: everything still proves, by the same tactic,
    but the text the kernel accepted is not the same text."""
    moved = _problem(proof_sha256=sha256_text("something else"))
    (line,) = compare(_baseline(), _baseline(moved))
    assert "accepted proof text changed" in line
    assert line.startswith("p1:")


def test_a_changed_sealed_statement_is_reported_as_a_different_goal() -> None:
    """A moved `goal_src` means decomposition or pretty-printing changed, so the system is no
    longer proving the same proposition -- a strictly more serious finding than a changed proof,
    and the message says so rather than leaving them indistinguishable."""
    moved = _problem(goal_src_sha256=sha256_text("different statement"))
    (line,) = compare(_baseline(), _baseline(moved))
    assert "sealed statement changed" in line
    assert "different goal" in line


def test_a_changed_axiom_cone_is_reported() -> None:
    moved = _problem(axioms=("propext", "Classical.choice"))
    (line,) = compare(_baseline(), _baseline(moved))
    assert "axiom cone" in line
    assert "Classical.choice" in line


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"proved": False}, "proved True -> False"),
        ({"sealed": False}, "sealed True -> False"),
        ({"tactic": "nlinarith"}, "winning tactic omega -> nlinarith"),
    ],
)
def test_each_per_problem_field_is_compared(overrides: dict[str, object], expected: str) -> None:
    lines = compare(_baseline(), _baseline(_problem(**overrides)))
    assert any(expected in line for line in lines), lines


def test_a_reconfigured_policy_says_to_rerecord_rather_than_reporting_a_regression() -> None:
    """Changing the portfolio *should* invalidate the baseline. That is a changed experiment, not
    a regression, and the message has to distinguish them or every portfolio change reads as a
    breakage."""
    current = _baseline(policy_config_hash="b" * 64, tactics=("rfl",))
    lines = compare(_baseline(), current)
    assert any("policy config changed" in line for line in lines)
    assert any("re-record deliberately" in line for line in lines)


def test_a_changed_corpus_says_nothing_below_is_comparable() -> None:
    lines = compare(_baseline(), _baseline(corpus_sha256="d" * 64))
    assert any("the benchmark itself moved" in line for line in lines)


def test_a_vanished_problem_is_reported_rather_than_ignored() -> None:
    """The failure mode a naive dict comparison misses: if the gate silently stopped running a
    problem, every remaining entry would still match and the baseline would pass."""
    recorded = _baseline(_problem("p1"), _problem("p2"))
    lines = compare(recorded, _baseline(_problem("p1")))
    assert lines == ("p2: was in the baseline and is absent now",)


def test_a_new_problem_is_reported_too() -> None:
    recorded = _baseline(_problem("p1"))
    lines = compare(recorded, _baseline(_problem("p1"), _problem("p2")))
    assert lines == ("p2: is present now and was not in the baseline",)


def test_a_changed_artifact_is_reported() -> None:
    lines = compare(_baseline(), _baseline(artifact_sha256="e" * 64))
    assert any("materialized artifact changed" in line for line in lines)
    assert any("the file a user takes away" in line for line in lines)


def test_the_run_id_is_normalized_out_of_the_artifact_digest() -> None:
    """Every run stamps its own uuid into the artifact header, so digesting the raw source would
    make the baseline fail on every run for a reason that says nothing about behaviour."""
    a = "-- Materialized\n-- run: 3f2504e0-4f89-11d3-9a0c-0305e82c3301\nimport Mathlib\n"
    b = "-- Materialized\n-- run: 0e2f4053-98f4-3d11-c0a9-1030832c8e03\nimport Mathlib\n"
    assert a != b
    assert normalize_artifact(a) == normalize_artifact(b)
    # And nothing else is normalized away.
    c = b.replace("import Mathlib", "import Init")
    assert normalize_artifact(b) != normalize_artifact(c)


def test_a_baseline_round_trips_through_json(tmp_path) -> None:
    """The on-disk form is the artefact under review, so it has to survive a save/load without
    quietly dropping a field -- a `tuple` arriving back as a `list` would make every comparison
    fail for the wrong reason."""
    original = _baseline(_problem("p1"), _problem("p2", tactic=None, proof_sha256=None))
    path = tmp_path / "baseline.json"
    save(original, path)
    assert compare(original, load(path)) == ()
    assert load(path) == original
    # Readable, and stable enough to review in a diff.
    assert json.loads(path.read_text())["problems"][0]["id"] == "p1"
