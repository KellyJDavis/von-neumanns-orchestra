"""M3.12 -- the vendored informal statements: pinned, complete, and safe to render.

No infrastructure. They are rendered into every prover prompt as a docstring (or Kimina's
`# Problem:`), so what matters is that the file is exactly what was vendored, covers exactly the
formal corpus, and cannot break the Lean it is spliced into.
"""

from __future__ import annotations

import json

import pytest
from lean_agent_eval.suites.minif2f import INFORMAL_PATH, load_corpus, load_informal
from lean_agent_eval.suites.vendor_minif2f import INFORMAL_COMMIT, informal_digest


def test_the_informal_statements_match_their_recorded_digest() -> None:
    informal = load_informal()
    statements = {
        problem_id: {"split": informal.split_by_id[problem_id], "informal_statement": text}
        for problem_id, text in informal.by_id.items()
    }
    assert informal_digest({"statements": statements}) == informal.informal_sha256


def test_every_problem_has_one_informal_statement_in_the_same_split() -> None:
    """Exactly the formal corpus's ids and splits -- after the one explicit rename
    (`vendor_minif2f.INFORMAL_RENAMES`), which this is what holds to account."""
    assert {p.id: p.split for p in load_corpus().problems} == load_informal().split_by_id


def test_no_statement_can_close_the_docstring_it_is_rendered_into() -> None:
    assert all(text.strip() and "-/" not in text for text in load_informal().by_id.values())


def test_only_statements_were_vendored_never_the_informal_proofs() -> None:
    """Upstream ships informal proofs beside the statements; a prompt carrying one would hand the
    prover its answer."""
    records = json.loads(INFORMAL_PATH.read_text())["statements"].values()
    assert all(set(record) == {"split", "informal_statement"} for record in records)


def test_the_provenance_is_the_pinned_original_under_the_same_licence() -> None:
    assert load_informal().provenance == {
        "repo": "https://github.com/facebookresearch/miniF2F",
        "commit": INFORMAL_COMMIT,
        "license": "MIT",
        "copyright": "Copyright (c) Meta Platforms, Inc. and affiliates.",
    }


def test_a_missing_statement_raises_rather_than_changing_the_prompt() -> None:
    with pytest.raises(KeyError, match="no informal statement"):
        load_informal().for_problem("definitely_not_a_miniF2F_problem")
