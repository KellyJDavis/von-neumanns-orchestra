"""`ContextBuilder`: spec §6.6's context bands.

| Band | Content | Budget |
|---|---|---|
| 1 | Sealed goal, pretty-printed, plus base env | Never evicted |
| 2 | Kernel diagnostics, infotree-localized | Hard 30% **[measure]** |
| 3 | Retrieved premises, ranked | Truncated by rank |
| 4 | Proved ancestors and siblings, statements only | Statements before proofs |
| 5 | Error history, deduplicated by class | Oldest first |
| 6 | Reference-document passages | Lowest rank first |

> Two rules matter more than the bands. **Never summarize kernel output** -- truncate instead:
> keep the error head, truncate the goal state by hypothesis, elide the middle with an explicit
> size marker so the model can request more rather than hallucinate over the gap. And **keep bands
> 1-2 byte-stable across resamples**, or prefix caching is defeated and cost multiplies silently.

Both rules are enforced structurally here rather than left to whoever assembles a prompt.

**Never summarize** is why `truncate_kernel_output` exists and why there is no summarizing path at
all. A summary of a kernel error is a paraphrase produced by something that does not know what the
kernel meant; the model then reasons about the paraphrase. Truncation with a marker leaves the
model able to see that something was removed and how much.

**Byte-stability** is why bands are emitted in order and bands 1-2 are rendered without reference
to anything after them. If band 3 shrinks under budget pressure, the bytes of bands 1-2 must not
move by so much as a space -- a shifted prefix is a cache miss, and the cost of that shows up as a
larger bill rather than as a failure.

Token counting is injected rather than imported. Counting needs a tokenizer, tokenizers live in
`lean_agent_models`, and `models` already depends on `core` -- so the builder takes a
`Callable[[str], int]` and a caller supplies `ChatTokenizer.encode`'s length. A caller with no
tokenizer to hand can pass a character-based estimate and get proportional behaviour.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import IntEnum

#: Counts tokens in a rendered string. Injected; see the module docstring.
TokenCounter = Callable[[str], int]


class Band(IntEnum):
    """Spec §6.6's bands, numbered so that **higher evicts first**.

    An `IntEnum` because eviction order *is* the numeric order, and writing it as a separate
    ordering table would let the two disagree.
    """

    SEALED_GOAL = 1
    KERNEL_DIAGNOSTICS = 2
    PREMISES = 3
    RELATED_OBLIGATIONS = 4
    ERROR_HISTORY = 5
    REFERENCE_DOCS = 6


#: Band 2's share of the budget. Spec marks this **[measure]**, so it is a default rather than a
#: finding: nobody has measured what fraction of a context window kernel diagnostics deserve. It
#: is a *hard* cap either way -- an unbounded goal state can be enormous, and letting it crowd out
#: every retrieved premise is the failure the cap exists to prevent.
DEFAULT_KERNEL_FRACTION = 0.30

#: What an elision says. The size is in tokens because the budget is, and the marker is explicit
#: so the model can ask for more rather than hallucinate over the gap (§6.6's own words).
ELISION = "⟨{count} {unit} elided⟩"


@dataclass(frozen=True)
class RankedItem:
    """A band entry with a rank, for the bands spec says are truncated by rank."""

    text: str
    rank: float = 0.0


@dataclass(frozen=True)
class ContextInputs:
    """Everything a caller can offer. What actually lands is decided by the budget."""

    sealed_goal: str
    base_env: str
    kernel_diagnostics: tuple[str, ...] = ()
    premises: tuple[RankedItem, ...] = ()
    related_obligations: tuple[str, ...] = ()
    error_history: tuple[str, ...] = ()
    reference_docs: tuple[RankedItem, ...] = ()


@dataclass(frozen=True)
class BuiltContext:
    """The assembled context, and an account of what it cost.

    `evicted` and `truncated` are reported rather than silently applied: a run whose premises were
    all dropped is a different experiment from one where they fitted, and nothing downstream could
    tell without being told.
    """

    text: str
    bands: dict[Band, str]
    tokens: int
    evicted: tuple[Band, ...] = ()
    truncated: tuple[Band, ...] = ()

    def prefix(self) -> str:
        """Bands 1-2 -- the part that must stay byte-stable across resamples."""
        return "".join(self.bands[band] for band in (Band.SEALED_GOAL, Band.KERNEL_DIAGNOSTICS))


def truncate_kernel_output(text: str, max_tokens: int, count: TokenCounter) -> str:
    """Spec §6.6's rule, applied literally: keep the error head, truncate the goal state by
    hypothesis, elide the middle with an explicit size marker.

    "By hypothesis" means whole lines are dropped, never a line cut in half. A hypothesis sliced
    mid-type reads as a *different* hypothesis rather than as a missing one, and the model has no
    way to tell which it is looking at.

    The `⊢` goal line is kept whatever else goes: it is the thing being proved, and a diagnostic
    that dropped it in favour of hypotheses would be describing a problem it no longer states.
    """
    if count(text) <= max_tokens:
        return text

    lines = text.split("\n")
    goal_index = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("⊢")), len(lines)
    )
    head = lines[:1]
    hypotheses = lines[1:goal_index]
    tail = lines[goal_index:]

    kept: list[str] = []
    for line in hypotheses:
        candidate = "\n".join(
            [*head, *kept, line, ELISION.format(count=0, unit="hypotheses"), *tail]
        )
        if count(candidate) > max_tokens:
            break
        kept.append(line)

    dropped = len(hypotheses) - len(kept)
    if dropped <= 0:
        # Nothing to drop and still over budget: the head or the goal alone exceeds the cap, so
        # elide by line rather than pretending the cap was met.
        return _elide_by_line(lines, max_tokens, count)
    return "\n".join([*head, *kept, ELISION.format(count=dropped, unit="hypotheses"), *tail])


def _elide_by_line(lines: list[str], max_tokens: int, count: TokenCounter) -> str:
    """Last resort when even the head and goal do not fit: keep as many leading lines as fit and
    say how many went. Still truncation, still marked -- never a summary."""
    kept: list[str] = []
    for line in lines:
        candidate = "\n".join([*kept, line, ELISION.format(count=0, unit="lines")])
        if count(candidate) > max_tokens:
            break
        kept.append(line)
    dropped = len(lines) - len(kept)
    if dropped <= 0:
        return "\n".join(lines)
    return "\n".join([*kept, ELISION.format(count=dropped, unit="lines")])


@dataclass(frozen=True)
class ContextBuilder:
    """Assembles §6.6's bands within a token budget.

    Frozen and configuration-only: the same builder against the same inputs must produce the same
    bytes, which is the byte-stability rule restated as a property of this object.
    """

    count: TokenCounter
    budget_tokens: int
    kernel_fraction: float = DEFAULT_KERNEL_FRACTION

    def build(self, inputs: ContextInputs) -> BuiltContext:
        bands: dict[Band, str] = {}
        truncated: list[Band] = []

        # Band 1 first and never evicted. Rendered from its own inputs alone, so nothing that
        # happens below can move a byte of it.
        bands[Band.SEALED_GOAL] = self._render_goal(inputs)

        # Band 2, capped hard. Also rendered independently of bands 3-6, for the same reason.
        kernel_cap = int(self.budget_tokens * self.kernel_fraction)
        kernel, was_truncated = self._render_kernel(inputs, kernel_cap)
        bands[Band.KERNEL_DIAGNOSTICS] = kernel
        if was_truncated:
            truncated.append(Band.KERNEL_DIAGNOSTICS)

        prefix_tokens = self.count(bands[Band.SEALED_GOAL] + bands[Band.KERNEL_DIAGNOSTICS])
        remaining = self.budget_tokens - prefix_tokens

        # Bands 3-6, each filled while there is room. Filled in band order so a higher-priority
        # band is never squeezed by a lower one that happened to be rendered first.
        evicted: list[Band] = []
        for band, rendered in (
            (Band.PREMISES, self._render_ranked("Premises", inputs.premises)),
            (
                Band.RELATED_OBLIGATIONS,
                self._render_lines("Related obligations", inputs.related_obligations),
            ),
            (
                Band.ERROR_HISTORY,
                self._render_lines("Error history", _dedupe(inputs.error_history)),
            ),
            (Band.REFERENCE_DOCS, self._render_ranked("References", inputs.reference_docs)),
        ):
            if not rendered:
                bands[band] = ""
                continue
            cost = self.count(rendered)
            if cost <= remaining:
                bands[band] = rendered
                remaining -= cost
            else:
                # Evicted whole rather than half-included. A band cut mid-entry reads as a
                # complete list that happens to be short, which is worse than an absent one.
                bands[band] = ""
                evicted.append(band)

        text = "".join(bands[band] for band in Band)
        return BuiltContext(
            text=text,
            bands=bands,
            tokens=self.count(text),
            evicted=tuple(evicted),
            truncated=tuple(truncated),
        )

    def _render_goal(self, inputs: ContextInputs) -> str:
        return f"# Goal\n{inputs.sealed_goal}\n\n# Environment\n{inputs.base_env}\n\n"

    def _render_kernel(self, inputs: ContextInputs, cap: int) -> tuple[str, bool]:
        if not inputs.kernel_diagnostics:
            return "", False
        body = "\n\n".join(inputs.kernel_diagnostics)
        truncated = truncate_kernel_output(body, cap, self.count)
        return f"# Kernel diagnostics\n{truncated}\n\n", truncated != body

    def _render_ranked(self, heading: str, items: Sequence[RankedItem]) -> str:
        if not items:
            return ""
        # Highest rank first, and ties broken by the caller's order so the render is deterministic
        # rather than dependent on sort stability being someone's assumption.
        ordered = sorted(items, key=lambda item: -item.rank)
        return f"# {heading}\n" + "\n".join(item.text for item in ordered) + "\n\n"

    def _render_lines(self, heading: str, lines: Sequence[str]) -> str:
        if not lines:
            return ""
        return f"# {heading}\n" + "\n".join(lines) + "\n\n"


def _dedupe(entries: Sequence[str]) -> tuple[str, ...]:
    """Band 5 is "deduplicated by class", and the class of an error is its first line.

    Oldest first, per spec's own eviction note for this band -- so the *first* occurrence of a
    class is the one kept, which is the one whose context the model has already seen least of.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for entry in entries:
        signature = entry.split("\n", 1)[0]
        if signature in seen:
            continue
        seen.add(signature)
        kept.append(entry)
    return tuple(kept)
