"""M1.9 exit criterion: exact-match (post-normalization) contamination detection -- pure logic,
no external infrastructure needed. See contamination.py's own docstring for why exact match,
deliberately, not fuzzy/semantic matching.
"""

from __future__ import annotations

from lean_agent_eval.contamination import find_contamination, normalize_statement


def test_normalize_strips_line_and_block_comments() -> None:
    with_line_comment = "theorem t : True := trivial -- easy\n"
    with_block_comment = "theorem t : True /- a proposition -/ := trivial"
    assert normalize_statement(with_line_comment) == "theorem t : True := trivial"
    assert normalize_statement(with_block_comment) == "theorem t : True := trivial"


def test_normalize_collapses_whitespace_variance() -> None:
    spaced_out = "theorem   t :\n\n  True :=\ttrivial"
    assert normalize_statement(spaced_out) == "theorem t : True := trivial"


def test_find_contamination_flags_exact_matches_after_normalization() -> None:
    eval_statements = {
        "prob_1": "theorem t : True := trivial -- from the benchmark\n",
        "prob_2": "theorem t : (1 : Nat) + 1 = 2 := by decide",
    }
    corpus = ["theorem t : True := trivial", "some unrelated declaration"]

    result = find_contamination(eval_statements, corpus)

    assert result == {"prob_1": eval_statements["prob_1"]}


def test_find_contamination_reports_nothing_for_a_clean_corpus() -> None:
    eval_statements = {"prob_1": "theorem t : True := trivial"}
    corpus = ["theorem completely_different : False -> True := fun h => h.elim"]

    assert find_contamination(eval_statements, corpus) == {}
