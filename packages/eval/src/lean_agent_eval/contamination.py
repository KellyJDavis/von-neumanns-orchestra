"""Contamination checking (spec §7.5) -- exact-match detection, deliberately, not fuzzy/semantic
matching. Semantic contamination detection (embeddings, paraphrase models) needs a model, and
this codebase has none yet (Phase 3 doesn't exist); building fuzzy matching now would mean either
faking that dependency or shipping something untested against the real thing it's for. Exact
match after normalization is the honest, buildable slice: it catches a benchmark statement copied
verbatim (or differing only in whitespace/comments) into a corpus, which is the common, cheap-to-
miss case that motivates checking for this at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# Strips Lean's two comment forms and collapses whitespace runs, so two statements that differ
# only in formatting or commentary normalize identically. Deliberately simple (no nested
# block-comment handling, no string-literal awareness) -- a Lean statement in a benchmark corpus
# is not going to contain a string literal that happens to look like a comment delimiter, and
# handling that correctly would need a real Lean tokenizer for a case that doesn't arise in
# practice here.
_LINE_COMMENT = re.compile(r"--.*")
_BLOCK_COMMENT = re.compile(r"/-.*?-/", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")


def normalize_statement(source: str) -> str:
    without_comments = _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", source))
    return _WHITESPACE.sub(" ", without_comments).strip()


def find_contamination(
    eval_statements: Mapping[str, str], corpus_statements: Iterable[str]
) -> dict[str, str]:
    """Return `{problem_id: matching_statement}` for every eval problem whose normalized
    statement exactly matches something in `corpus_statements` -- e.g. flagging a benchmark
    problem that was copied verbatim into a training or prompt corpus.
    """
    normalized_corpus = {normalize_statement(s) for s in corpus_statements}
    return {
        problem_id: statement
        for problem_id, statement in eval_statements.items()
        if normalize_statement(statement) in normalized_corpus
    }
