"""Internal regression suite (spec §7.5's table: "Symbolic-only, deterministic, zero tokens,
every PR"). A small, hand-authored set of *complete* Lean developments (statement and proof
together -- no policy/agent involved, since none exists yet) exercised against a real
`leankernel serve` process (M1.8.1/M1.8.2) to catch a regression in the acceptance mechanism
itself, not to evaluate a prover's capability.

Runs through `ReplWorker` directly, not the full `pool`/`cache`/`api` stack
`tests/leanserv/test_api.py` already exercises against real Postgres -- this suite is meant to be
the cheapest possible "did the core checking mechanism regress" signal (Lean toolchain only, no
database), matching spec's "every PR" cadence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from lean_agent_core.enums import VerdictKind
from lean_agent_serv.repl import DEFAULT_TIMEOUT_MS, ReplCrashed, ReplTimeout, ReplWorker

from lean_agent_eval.score import AttemptOutcome


@dataclass(frozen=True)
class SuiteProblem:
    id: str
    development: str
    imports: tuple[str, ...] = ("Init",)


# Two genuinely closed goals, one genuinely false statement (must be rejected, not merely
# "expected to fail" by convention -- mirrors Phase 1 gate 2/3's "zero false negatives" spirit at
# the elaboration level), and one `sorry`-as-proof case that deliberately documents a real,
# current limitation rather than a bug: `check` is plain elaboration, not the full seal/link/
# replay/audit acceptance path (spec §4) -- it has no axiom audit, so a `sorry`'d proof elaborates
# with only a *warning* (confirmed empirically in M1.1: `sorryAx` never raises a message-log
# error), and comes back `ok=True` here. `expect_ok` records what `check` actually does today, not
# what a full acceptance check would eventually do once `/v1/link` exists.
INTERNAL_SUITE: tuple[SuiteProblem, ...] = (
    SuiteProblem(id="decide_add", development="theorem t : (2 : Nat) + 3 = 3 + 2 := by decide"),
    SuiteProblem(
        id="decide_mul_assoc",
        development="theorem t : (2 * 3) * 4 = 2 * (3 * 4 : Nat) := by decide",
    ),
    SuiteProblem(
        id="false_statement_is_rejected",
        development="theorem t : (1 : Nat) + 1 = 3 := by decide",
    ),
    SuiteProblem(
        id="sorry_is_not_audited_by_plain_check",
        development="theorem t : (1 : Nat) + 1 = 2 := by sorry",
    ),
)

# Whether `check`'s `ok` is expected to be true for each problem above, by id -- the suite runner
# doesn't assert this itself (producing outcomes and judging them are separate concerns); see
# tests/eval/test_internal_suite.py for the assertions against this table.
EXPECTED_OK: dict[str, bool] = {
    "decide_add": True,
    "decide_mul_assoc": True,
    "false_statement_is_rejected": False,
    "sorry_is_not_audited_by_plain_check": True,
}


async def run_internal_suite(
    lake_project_dir: Path, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
) -> dict[str, AttemptOutcome]:
    """Run every `INTERNAL_SUITE` problem once, reusing one warm worker per distinct `imports`
    tuple (all of them share `("Init",)` today, so in practice this spawns exactly one) rather
    than one worker per problem -- not `pool.py`'s full mutual-exclusion machinery, since this
    runner is strictly sequential and has no concurrent callers to protect a shared worker from.
    """
    results: dict[str, AttemptOutcome] = {}
    workers: dict[tuple[str, ...], ReplWorker] = {}
    try:
        for problem in INTERNAL_SUITE:
            worker = workers.get(problem.imports)
            if worker is None or not worker.is_alive:
                worker = await ReplWorker.spawn(lake_project_dir, problem.imports)
                workers[problem.imports] = worker

            started = time.monotonic()
            try:
                check_result = await worker.check(problem.development, timeout_ms=timeout_ms)
                kind = VerdictKind.PROVED if check_result.ok else VerdictKind.ERRORS
            except ReplTimeout:
                kind = VerdictKind.TIMEOUT
            except ReplCrashed:
                kind = VerdictKind.INFRA_ERROR
            elapsed_ms = int((time.monotonic() - started) * 1000)

            # tokens=0 always: spec's own "zero tokens" for this suite is literally true, no
            # model is ever called. kernel_ms is left at 0 -- Serve.lean doesn't report kernel
            # time separately from overall elapsed time, a known simplification, not an oversight.
            results[problem.id] = AttemptOutcome(
                kind=kind, tokens=0, kernel_ms=0, wallclock_ms=elapsed_ms
            )
    finally:
        for worker in workers.values():
            await worker.close()
    return results
