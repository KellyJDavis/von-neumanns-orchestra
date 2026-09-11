"""Kernel diagnostics, turned into what a repair prompt shows the model (spec §6.6 band 2).

Two jobs, both about keeping the kernel's own words intact on their way to a model.

**Reading positions.** `/v1/check` returns each diagnostic as Lean renders it --
`<input>:L:C-L':C': error: message` since M3.10 (`Serve.lean` passes `includeEndPos`). The prefix is
Lean's documented `mkErrorStringWithPos` format, so reading it is reading Lean's *output*, which is
different in kind from the regex-over-Lean-source this codebase refuses: nothing about the meaning
of a program is inferred here, only where the kernel said it complained.

**Rendering them the way a trained prover expects.** Goedel-Prover-V2 was trained to repair against
a specific rendering (its `get_error_str`): for each of at most 8 errors, the offending span marked
with `<error></error>` inside the model's own code, four lines before it and one after, then
`Error Message: ...`. This module reimplements that rendering from its observed behaviour -- the
repository declares Apache-2.0 in its README but ships no LICENSE file, so its code is not copied
here -- and `tests/test_repair_feedback.py` pins every rule of it.

Where this system's rules differ from Goedel's harness they win, and each difference is stated:

* **Kernel output is truncated, never summarized** (spec §6.6), and band 2 is hard-capped. Goedel's
  harness includes every message whole; a message here is cut by `truncate_kernel_output` -- error
  head and `⊢` kept, hypotheses dropped whole, the gap marked with a count.
* **Positions are mapped back to the model's own code.** The diagnostic is positioned in the
  development, where the model's code sits after a header it never wrote. An error that falls
  outside the model's code (in the entry that links it to the sealed goal -- a renamed or
  restated theorem) has no code of the model's to point at, so it is shown as its message alone.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from lean_agent_core.context import truncate_kernel_output

#: Lean's `mkErrorStringWithPos`, anchored at the start: file, `line:column`, optional
#: `-endLine:endColumn`, then a severity word (with an optional error name, `error(lean.x)`) --
#: absent for an information message, which Lean renders with no severity at all.
_POSITIONED = re.compile(
    r"^[^:\n]*:(\d+):(\d+)(?:-(\d+):(\d+))?: (?:(error|warning)(?:\([^)\n]*\))?: )?",
)

#: `get_error_str`'s limit (its `error_thres`): past eight errors, the rest are counted, not shown.
MAX_ERRORS = 8

#: Lines of the model's code shown before an error, and the most shown *inside* a multi-line one
#: before the span is truncated. Both are `get_error_str`'s.
_BEFORE = 4
_SPAN_LINES = 6


def estimate_tokens(text: str) -> int:
    """A deliberately pessimistic token count, for a policy that has no tokenizer.

    Measured under Goedel-Prover-V2's tokenizer (= Qwen3's): Lean code and prose run at ~2.2
    characters per token, and a diagnostic dense with `ℝ`, `⊢` and subscripts at 1.7. Dividing by
    1.5 overestimates all of them, which is the safe direction: an overestimate truncates a little
    early, an underestimate could send a prompt past the server's context and turn a repair into a
    rejected request.
    """
    return math.ceil(len(text) / 1.5)


@dataclass(frozen=True)
class Diagnostic:
    """One Lean message: where it is (1-based lines, 0-based columns, as Lean counts them), how
    severe, and what it says without its position prefix."""

    severity: str
    line: int
    column: int
    end_line: int | None
    end_column: int | None
    message: str


def parse_diagnostic(text: str) -> Diagnostic | None:
    """Split a rendered Lean message into position, severity and text; `None` when it carries no
    position at all (an exception rendered as text, a worker-level failure)."""
    match = _POSITIONED.match(text)
    if match is None:
        return None
    line, column, end_line, end_column, severity = match.groups()
    return Diagnostic(
        severity=severity or "information",
        line=int(line),
        column=int(column),
        end_line=int(end_line) if end_line is not None else None,
        end_column=int(end_column) if end_column is not None else None,
        message=text[match.end() :].rstrip("\n"),
    )


def errors_to_show(diagnostics: Sequence[str]) -> list[Diagnostic | str]:
    """The diagnostics that explain a rejection: its errors, or -- when there are none -- all of it.

    The fallback is not cosmetic. A candidate the executor screened out for `sorryAx` elaborated
    *cleanly*: its only message is the warning "declaration uses 'sorry'", and a repair prompt
    built from errors alone would tell the model its proof failed and then show it nothing.
    Unpositioned text is kept as text rather than dropped.
    """
    parsed = [(text, parse_diagnostic(text)) for text in diagnostics]
    errors = [d for _, d in parsed if d is not None and d.severity == "error"]
    if errors:
        return list(errors)
    return [d if d is not None else text for text, d in parsed]


def render_errors(
    code: str,
    line_offset: int,
    diagnostics: Sequence[str],
    *,
    message_budget_tokens: int,
    max_errors: int = MAX_ERRORS,
) -> str:
    """Goedel-Prover-V2's error rendering, over this system's diagnostics.

    `code` is the model's code exactly as it sits in the development, and `line_offset` the number
    of development lines before it (`WholeProofSampler.development_parts`). `message_budget_tokens`
    is band 2's share of the prompt, split evenly across the errors shown.
    """
    shown = errors_to_show(diagnostics)
    visible = shown[:max_errors]
    per_message = max(1, message_budget_tokens // max(1, len(visible)))
    code_lines = code.split("\n")

    rendered = ""
    for index, item in enumerate(visible, start=1):
        rendered += f"\nError {index}:\n"
        if isinstance(item, str):
            message = item.rstrip("\n")
        else:
            message = item.message
            snippet = _snippet(code_lines, item, line_offset)
            if snippet is not None:
                rendered += "\nCorresponding Code:\n```lean4\n" + snippet + "\n```\n"
        message = truncate_kernel_output(message, per_message, estimate_tokens)
        rendered += f"\nError Message: {message}\n"

    if len(shown) > max_errors:
        rendered += f"\n... [Omitted {len(shown) - max_errors} more errors] ...\n"
    return rendered


def _snippet(code_lines: list[str], error: Diagnostic, line_offset: int) -> str | None:
    """The model's code around one error, the span wrapped in `<error></error>`; `None` when the
    error is not in the model's code at all."""
    start_line = error.line - line_offset - 1
    if not 0 <= start_line < len(code_lines):
        return None
    start_col = error.column
    end_line = error.end_line - line_offset - 1 if error.end_line is not None else start_line
    if error.end_line is None or not start_line <= end_line < len(code_lines):
        # No end, or an end outside the model's code: mark to the end of the start line, which is
        # what the reference does for a message without an end position.
        end_line = start_line
        end_col = len(code_lines[start_line])
    else:
        end_col = error.end_column if error.end_column is not None else len(code_lines[end_line])

    out = ""
    for line in code_lines[max(0, start_line - _BEFORE) : start_line]:
        out += f"{line}\n"
    first = code_lines[start_line]
    if start_line != end_line:
        out += first[:start_col] + "<error>" + first[start_col:] + "\n"
        last_shown = min(end_line, start_line + _SPAN_LINES)
        for line in code_lines[start_line + 1 : last_shown]:
            out += f"{line}\n"
        if end_line > start_line + _SPAN_LINES:
            anchor = code_lines[last_shown - 1]
            indent = len(anchor) - len(anchor.lstrip(" "))
            out += "\n" + " " * indent + "... --[Truncated]-- ...\n"
        last = code_lines[end_line]
        out += last[:end_col] + "</error>" + last[end_col:] + "\n"
    else:
        out += first[:start_col] + "<error>" + first[start_col:end_col] + "</error>"
        out += first[end_col:] + "\n"
    if end_line + 1 < len(code_lines):
        out += f"{code_lines[end_line + 1]}\n"
    # Returned with its trailing newline: the reference appends "\n```" after it, so every snippet
    # ends in a blank line before the closing fence -- a detail a prover trained on it has seen
    # every single time.
    return out
