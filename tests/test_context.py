"""M3.8 -- `ContextBuilder`, spec §6.6's bands.

No infrastructure. Spec is explicit that two rules matter more than the bands themselves, so most
of what follows is those two: kernel output is truncated and never summarized, and bands 1-2 stay
byte-stable across resamples. The second is the one with a silent failure mode -- a shifted prefix
defeats prefix caching and shows up as a larger bill rather than as a broken test -- so it is
tested as a property across changes to everything else.
"""

from __future__ import annotations

import pytest
from lean_agent_core.context import (
    DEFAULT_KERNEL_FRACTION,
    Band,
    BuiltContext,
    ContextBuilder,
    ContextInputs,
    RankedItem,
    truncate_kernel_output,
)


def words(text: str) -> int:
    """A token counter standing in for a tokenizer.

    Whitespace-delimited rather than character-based, so the numbers in these tests are readable
    and the budget arithmetic is exact. `ContextBuilder` takes any `Callable[[str], int]` -- a real
    caller passes `len(ChatTokenizer.encode(...))`.
    """
    return len(text.split())


#: A real-shaped Lean diagnostic: an error head, five hypotheses, and the goal.
GOAL_STATE = """error: linarith failed to find a contradiction
x : ℝ
y : ℝ
h₀ : 0 < x
h₁ : x < y
h₂ : y < 1
⊢ x < 1"""


def _inputs(**overrides: object) -> ContextInputs:
    base: dict[str, object] = {
        "sealed_goal": "∀ (x : ℝ), 0 < x → x ≤ 1",
        "base_env": "Mathlib",
    }
    base.update(overrides)
    return ContextInputs(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------
# Rule one: never summarize kernel output.
# --------------------------------------------------------------------------------------------


def test_kernel_output_is_truncated_by_hypothesis_never_mid_line() -> None:
    """ "By hypothesis" is the load-bearing part. A hypothesis sliced mid-type reads as a
    *different* hypothesis rather than as a missing one, and the model cannot tell which it is
    looking at."""
    truncated = truncate_kernel_output(GOAL_STATE, max_tokens=20, count=words)

    kept = [line for line in truncated.split("\n") if "elided" not in line]
    original = GOAL_STATE.split("\n")
    for line in kept:
        assert line in original, f"{line!r} is not a whole line from the original"


def test_the_error_head_and_the_goal_both_survive() -> None:
    """Spec says keep the error head. The `⊢` line is kept for a reason of its own: it is the
    thing being proved, and a diagnostic that dropped it in favour of hypotheses would describe a
    problem it no longer states."""
    truncated = truncate_kernel_output(GOAL_STATE, max_tokens=15, count=words)

    assert truncated.startswith("error: linarith failed")
    assert "⊢ x < 1" in truncated


def test_the_elision_says_how_much_went() -> None:
    """ "An explicit size marker so the model can request more rather than hallucinate over the
    gap" -- so the marker has to carry a number, not just say something was cut."""
    truncated = truncate_kernel_output(GOAL_STATE, max_tokens=15, count=words)

    marker = next(line for line in truncated.split("\n") if "elided" in line)
    assert "hypotheses" in marker
    dropped = int(marker.split()[0].lstrip("⟨"))
    assert dropped > 0
    kept_hypotheses = [
        line for line in truncated.split("\n") if line.startswith(("x :", "y :", "h"))
    ]
    assert dropped + len(kept_hypotheses) == 5, "every hypothesis is either kept or counted"


def test_output_that_fits_is_returned_untouched() -> None:
    """No marker, no reflow, no normalization -- byte-identical, because band 2 has to be stable
    across resamples and any cosmetic pass here would break that."""
    assert truncate_kernel_output(GOAL_STATE, max_tokens=1000, count=words) == GOAL_STATE


def test_nothing_is_ever_paraphrased() -> None:
    """The rule stated as a property: every non-marker line of the output appears verbatim in the
    input. A summarizing implementation could not satisfy this."""
    for cap in (5, 10, 15, 20, 30):
        truncated = truncate_kernel_output(GOAL_STATE, max_tokens=cap, count=words)
        for line in truncated.split("\n"):
            assert "elided" in line or line in GOAL_STATE.split("\n")


def test_an_oversized_head_still_elides_rather_than_overflowing() -> None:
    """When even the head and goal do not fit, the answer is still truncation with a marker --
    not a summary, and not silently exceeding the cap."""
    huge = "error: " + " ".join(f"tok{i}" for i in range(200)) + "\n⊢ True"
    truncated = truncate_kernel_output(huge, max_tokens=10, count=words)
    assert "elided" in truncated


# --------------------------------------------------------------------------------------------
# Rule two: bands 1-2 byte-stable across resamples.
# --------------------------------------------------------------------------------------------


def test_the_prefix_is_identical_across_repeated_builds() -> None:
    builder = ContextBuilder(count=words, budget_tokens=200)
    inputs = _inputs(kernel_diagnostics=(GOAL_STATE,))
    assert builder.build(inputs).prefix() == builder.build(inputs).prefix()


@pytest.mark.parametrize(
    "changed",
    [
        {"premises": (RankedItem("Nat.add_comm", 0.9),)},
        {"related_obligations": ("theorem sibling : True", "theorem ancestor : True")},
        {"error_history": ("error: omega failed",)},
        {"reference_docs": (RankedItem("Mathlib docs: linarith", 0.2),)},
    ],
)
def test_the_prefix_does_not_move_when_later_bands_change(changed: dict[str, object]) -> None:
    """The rule that actually bites. Bands 3-6 vary between resamples -- a new error joins the
    history, a different premise is retrieved -- and if that shifted bands 1-2 by even one byte,
    every resample would be a prefix-cache miss and the cost would multiply silently."""
    builder = ContextBuilder(count=words, budget_tokens=200)
    bare = builder.build(_inputs(kernel_diagnostics=(GOAL_STATE,)))
    enriched = builder.build(_inputs(kernel_diagnostics=(GOAL_STATE,), **changed))

    assert enriched.prefix() == bare.prefix()
    assert enriched.text.startswith(bare.prefix())


def test_the_prefix_does_not_move_when_lower_bands_overflow_the_budget() -> None:
    """The rule under the condition that causes eviction, at a *fixed* budget.

    This is the scenario the rule is about: the same obligation resampled, where band 3 has grown
    enough to be dropped. A builder that reclaimed room by shrinking bands 1-2 would be defeating
    the cache to save tokens it should not be saving.

    Note the scope, which an earlier version of this test got wrong by asserting too much: the
    prefix is stable across *resamples*, not across *reconfigurations*. Band 2's cap is a fraction
    of the total budget, so changing `budget_tokens` legitimately changes it -- and that is not a
    resample, it is a different experiment.
    """
    builder = ContextBuilder(count=words, budget_tokens=60)
    light = builder.build(_inputs(kernel_diagnostics=(GOAL_STATE,)))
    heavy = builder.build(
        _inputs(
            kernel_diagnostics=(GOAL_STATE,),
            premises=tuple(RankedItem(f"premise {i} " * 10, 0.5) for i in range(20)),
        )
    )

    assert heavy.evicted, "this much lower-band content should force an eviction"
    assert heavy.prefix() == light.prefix()


def test_band_two_scales_with_the_budget_which_is_a_reconfiguration_not_a_resample() -> None:
    """The other side of the scope above, asserted rather than left implicit: a larger budget
    gives band 2 a larger share, so its bytes differ. That is correct -- the two runs are not
    resamples of each other -- and pinning it here stops someone "fixing" the stability rule by
    making the cap absolute and quietly capping a 200k-token context at 30% of a small default."""
    inputs = _inputs(kernel_diagnostics=(GOAL_STATE,))
    cramped = ContextBuilder(count=words, budget_tokens=30).build(inputs)
    roomy = ContextBuilder(count=words, budget_tokens=10_000).build(inputs)

    assert cramped.prefix() != roomy.prefix()
    assert Band.KERNEL_DIAGNOSTICS in cramped.truncated
    assert Band.KERNEL_DIAGNOSTICS not in roomy.truncated


# --------------------------------------------------------------------------------------------
# The bands themselves.
# --------------------------------------------------------------------------------------------


def test_band_one_is_never_evicted_however_small_the_budget() -> None:
    """ "Never evicted", because without it there is nothing to prove."""
    built = ContextBuilder(count=words, budget_tokens=1).build(
        _inputs(
            kernel_diagnostics=(GOAL_STATE,),
            premises=(RankedItem("Nat.add_comm", 0.9),),
            reference_docs=(RankedItem("docs", 0.1),),
        )
    )
    assert "∀ (x : ℝ), 0 < x → x ≤ 1" in built.bands[Band.SEALED_GOAL]
    assert Band.SEALED_GOAL not in built.evicted


def test_kernel_diagnostics_are_capped_at_their_share_of_the_budget() -> None:
    """Spec's hard 30% (marked [measure], so a default rather than a finding). An unbounded goal
    state can be enormous, and letting it crowd out every retrieved premise is what the cap
    prevents."""
    enormous = "error: boom\n" + "\n".join(f"h{i} : Nat" for i in range(500)) + "\n⊢ False"
    builder = ContextBuilder(count=words, budget_tokens=100)
    built = builder.build(_inputs(kernel_diagnostics=(enormous,)))

    assert Band.KERNEL_DIAGNOSTICS in built.truncated
    assert words(built.bands[Band.KERNEL_DIAGNOSTICS]) <= 100 * DEFAULT_KERNEL_FRACTION + 5


def test_eviction_takes_the_lowest_priority_band_first() -> None:
    """Bands are numbered so that higher evicts first, and `Band` is an `IntEnum` precisely so the
    ordering cannot drift from a separate table."""
    inputs = _inputs(
        premises=(RankedItem("premise " * 20, 0.9),),
        reference_docs=(RankedItem("reference " * 20, 0.9),),
    )
    built = ContextBuilder(count=words, budget_tokens=40).build(inputs)

    assert Band.REFERENCE_DOCS in built.evicted
    assert Band.PREMISES not in built.evicted


def test_premises_are_ordered_by_rank() -> None:
    built = ContextBuilder(count=words, budget_tokens=1000).build(
        _inputs(
            premises=(
                RankedItem("low", 0.1),
                RankedItem("high", 0.9),
                RankedItem("middle", 0.5),
            )
        )
    )
    rendered = built.bands[Band.PREMISES]
    assert rendered.index("high") < rendered.index("middle") < rendered.index("low")


def test_error_history_is_deduplicated_by_class_keeping_the_oldest() -> None:
    """ "Deduplicated by class", evicted "oldest first" -- so the first occurrence of a class is
    the one kept, since it is the one whose context the model has seen least of."""
    built = ContextBuilder(count=words, budget_tokens=1000).build(
        _inputs(
            error_history=(
                "error: omega failed\nfirst occurrence",
                "error: linarith failed\nsomething else",
                "error: omega failed\nsecond occurrence",
            )
        )
    )
    rendered = built.bands[Band.ERROR_HISTORY]
    assert "first occurrence" in rendered
    assert "second occurrence" not in rendered
    assert "linarith failed" in rendered


def test_a_band_is_evicted_whole_rather_than_cut_in_half() -> None:
    """A band cut mid-entry reads as a complete list that happens to be short, which is worse than
    an absent one -- the model would take it for everything there is."""
    built = ContextBuilder(count=words, budget_tokens=45).build(
        _inputs(
            premises=(
                RankedItem("aaa " * 30, 0.9),
                RankedItem("bbb " * 30, 0.5),
            )
        )
    )
    assert built.bands[Band.PREMISES] == ""
    assert Band.PREMISES in built.evicted


def test_what_was_dropped_is_reported_not_silently_applied() -> None:
    """A run whose premises were all evicted is a different experiment from one where they fitted,
    and nothing downstream could tell without being told."""
    built = ContextBuilder(count=words, budget_tokens=50).build(
        _inputs(
            kernel_diagnostics=(GOAL_STATE,),
            premises=(RankedItem("premise " * 40, 0.9),),
        )
    )
    assert isinstance(built, BuiltContext)
    assert built.evicted or built.truncated
    assert set(built.evicted) <= set(Band)


def test_empty_bands_contribute_nothing_rather_than_an_empty_heading() -> None:
    """An empty "# Premises" heading tells the model a search happened and found nothing, which is
    a different claim from not having searched."""
    built = ContextBuilder(count=words, budget_tokens=1000).build(_inputs())
    assert built.bands[Band.PREMISES] == ""
    assert "Premises" not in built.text
    assert "Kernel diagnostics" not in built.text
