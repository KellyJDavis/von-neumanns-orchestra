"""M3.10 -- kernel diagnostics rendered the way Goedel-Prover-V2 was trained to repair against.

No infrastructure. `feedback.render_errors` reimplements Goedel's `get_error_str` from its observed
behaviour (the authors' repository declares Apache-2.0 but ships no LICENSE file, so its code is not
in this one). During development the two were compared byte-for-byte on seven cases -- single-line,
multi-line, truncated span, no end position, first and last line, more than eight errors -- and
agreed on every one; these tests pin each of those rules without needing the reference.
"""

from __future__ import annotations

import json
from pathlib import Path

from lean_agent_policies.feedback import (
    Diagnostic,
    errors_to_show,
    estimate_tokens,
    parse_diagnostic,
    render_errors,
)
from tokenizers import Tokenizer

CODE_LINES = [
    "theorem G_1 : ∀ (n : ℕ), 0 < n → (21 * n + 4).gcd (14 * n + 3) = 1 := by",  # 1
    "  intro n hn",  # 2
    "  have h₁ : Nat.gcd (21 * n + 4) (14 * n + 3) = Nat.gcd (14 * n + 3) (7 * n + 1) := by",  # 3
    "    rw [show 21 * n + 4 = 1 * (14 * n + 3) + (7 * n + 1) by ring]",  # 4
    "    simp [Nat.gcd_comm]",  # 5
    "  have h₂ : Nat.gcd (14 * n + 3) (7 * n + 1) = 1 := by",  # 6
    "    have h₃ : 14 * n + 3 = 2 * (7 * n + 1) + 1 := by omega",  # 7
    "    rw [h₃]",  # 8
    "    simp",  # 9
    "    omega",  # 10
    "    linarith",  # 11
    "    nlinarith",  # 12
    "    norm_num",  # 13
    "  rw [h₁, h₂]",  # 14
]
CODE = "\n".join(CODE_LINES)
UNBOUNDED = 10**9


def diag(line: int, col: int, end: tuple[int, int] | None, message: str) -> str:
    """A diagnostic exactly as `/v1/check` returns it since M3.10."""
    end_part = "" if end is None else f"-{end[0]}:{end[1]}"
    return f"<input>:{line}:{col}{end_part}: error: {message}\n"


# --------------------------------------------------------------------------------------------
# Reading Lean's own rendering.
# --------------------------------------------------------------------------------------------


def test_real_check_diagnostics_parse() -> None:
    """Both strings are verbatim `/v1/check` output from a real `Init` worker, including the error
    name Lean 4.33 puts in parentheses after the severity."""
    omega = parse_diagnostic(
        "<input>:7:2-7:7: error: omega could not prove the goal:\na possible counterexample may "
        "satisfy the constraints\n  a ≥ 1\nwhere\n a := ↑n\n"
    )
    assert omega == Diagnostic(
        severity="error",
        line=7,
        column=2,
        end_line=7,
        end_column=7,
        message="omega could not prove the goal:\na possible counterexample may satisfy the "
        "constraints\n  a ≥ 1\nwhere\n a := ↑n",
    )
    unknown = parse_diagnostic(
        "<input>:11:8-11:11: error(lean.unknownIdentifier): Unknown identifier `foo`\n"
    )
    assert unknown is not None
    assert (unknown.severity, unknown.message) == ("error", "Unknown identifier `foo`")


def test_a_diagnostic_cached_before_end_positions_still_parses() -> None:
    parsed = parse_diagnostic("<input>:7:2: error: omega could not prove the goal\n")
    assert parsed is not None and (parsed.end_line, parsed.end_column) == (None, None)


def test_warnings_information_and_unpositioned_text() -> None:
    warning = parse_diagnostic("<input>:3:8-3:11: warning: declaration uses 'sorry'\n")
    assert warning is not None and warning.severity == "warning"
    info = parse_diagnostic("<input>:1:0-1:5: 4\n")
    assert info is not None and (info.severity, info.message) == ("information", "4")
    assert parse_diagnostic("Lean worker crashed") is None


def test_only_errors_are_shown_when_there_are_any() -> None:
    shown = errors_to_show(
        [
            "<input>:2:0-2:3: warning: unused variable `h`\n",
            diag(5, 4, (5, 8), "simp made no progress"),
        ]
    )
    assert [d.message for d in shown if isinstance(d, Diagnostic)] == ["simp made no progress"]


def test_a_sorry_rejection_is_explained_by_its_warning() -> None:
    """A candidate screened out for `sorryAx` elaborated cleanly, so it has no errors at all -- a
    prompt built from errors alone would say "your proof is wrong" and then show nothing."""
    (shown,) = errors_to_show(["<input>:8:4-8:9: warning: declaration uses 'sorry'\n"])
    assert isinstance(shown, Diagnostic) and "sorry" in shown.message


# --------------------------------------------------------------------------------------------
# The rendering rules.
# --------------------------------------------------------------------------------------------


def test_a_single_line_error_in_goedels_format() -> None:
    """Four lines before, the span wrapped in `<error></error>`, one line after, the fence closed
    after a blank line, then the message -- exactly as Goedel-Prover-V2 saw it in training."""
    rendered = render_errors(
        CODE, 0, [diag(8, 4, (8, 11), "rewrite failed")], message_budget_tokens=UNBOUNDED
    )
    assert rendered == (
        "\nError 1:\n"
        "\nCorresponding Code:\n```lean4\n"
        + "".join(f"{line}\n" for line in CODE_LINES[3:7])
        + "    <error>rw [h₃]</error>\n"
        + "    simp\n"
        + "\n```\n"
        + "\nError Message: rewrite failed\n"
    )


def test_positions_are_mapped_back_into_the_models_own_code() -> None:
    """The diagnostic is positioned in the development, where the model's code follows a header
    it never wrote. The same error five lines further down is the same error in its code."""
    shifted = render_errors(
        CODE, 5, [diag(13, 4, (13, 11), "rewrite failed")], message_budget_tokens=UNBOUNDED
    )
    direct = render_errors(
        CODE, 0, [diag(8, 4, (8, 11), "rewrite failed")], message_budget_tokens=UNBOUNDED
    )
    assert shifted == direct


def test_an_error_outside_the_models_code_is_shown_as_its_message_alone() -> None:
    """E.g. the entry's `exact`, when the model renamed or restated its theorem: there is no line
    of the model's to point at, and showing it code it never wrote would invite it to copy that
    code into its next answer."""
    rendered = render_errors(
        CODE,
        5,
        [diag(21, 50, (21, 70), "Unknown identifier `LeanAgent.Sol.G_1`")],
        message_budget_tokens=UNBOUNDED,
    )
    assert rendered == "\nError 1:\n\nError Message: Unknown identifier `LeanAgent.Sol.G_1`\n"


def test_a_long_span_is_truncated_inside_the_marker() -> None:
    rendered = render_errors(
        CODE, 0, [diag(6, 50, (13, 12), "unsolved goals")], message_budget_tokens=UNBOUNDED
    )
    assert "... --[Truncated]-- ...\n" in rendered
    assert "<error>" in rendered and "    norm_num</error>\n" in rendered
    # Five lines of the span are shown after its first line, then the marker, then its last line.
    assert "    nlinarith\n" not in rendered


def test_no_end_position_marks_to_the_end_of_the_line() -> None:
    rendered = render_errors(
        CODE, 0, [diag(5, 4, None, "simp made no progress")], message_budget_tokens=UNBOUNDED
    )
    assert "    <error>simp [Nat.gcd_comm]</error>\n" in rendered


def test_past_eight_errors_the_rest_are_counted_not_dropped() -> None:
    errors = [diag(line, 2, (line, 5), f"e{line}") for line in range(2, 13)]
    rendered = render_errors(CODE, 0, errors, message_budget_tokens=UNBOUNDED)
    assert rendered.count("\nError Message: ") == 8
    assert rendered.endswith("\n... [Omitted 3 more errors] ...\n")


def test_a_long_message_is_truncated_never_summarized() -> None:
    """Band 2's rule (spec §6.6) where Goedel's harness has none: the error head and the goal line
    survive, hypotheses go whole, and the gap is counted. Every kept line is the kernel's own."""
    hypotheses = [f"h{i} : x{i} * x{i} ≥ 0 ∧ x{i} + {i} > {i}" for i in range(60)]
    message = "linarith failed to find a contradiction\n" + "\n".join(hypotheses) + "\n⊢ False"
    rendered = render_errors(CODE, 0, [diag(11, 4, (11, 12), message)], message_budget_tokens=200)
    shown = rendered.split("\nError Message: ", 1)[1].rstrip("\n").split("\n")
    assert shown[0] == "linarith failed to find a contradiction"
    assert shown[-1] == "⊢ False"
    assert any("hypotheses elided" in line for line in shown)
    for line in shown:
        assert "elided" in line or line in message.split("\n")


def test_the_token_estimate_never_undercounts_real_prover_text() -> None:
    """`estimate_tokens` stands in for a tokenizer the policy does not have, and its one job is to
    err high. Held to Goedel-Prover-V2's real tokenizer over its real recorded output."""
    data = Path(__file__).parent / "models" / "data"
    tokenizer = Tokenizer.from_file(
        str(data / "tokenizers" / "Qwen3-0.6B" / "tokenizer.converted.json")
    )
    document = json.loads((data / "goedel_whole_proof.json").read_text())
    texts = [
        choice["text"]
        for interaction in document["interactions"]
        for choice in interaction["response"]["choices"]
    ] + [CODE, "⊢ ∀ (x y : ℝ), x ^ 2 + y ^ 2 ≥ 2 * x * y → ‖x‖ ≤ √(x ^ 2)"]
    for text in texts:
        actual = len(tokenizer.encode(text, add_special_tokens=False).ids)
        assert estimate_tokens(text) >= actual, (estimate_tokens(text), actual)
