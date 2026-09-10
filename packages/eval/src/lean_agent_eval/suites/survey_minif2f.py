"""Measure which miniF2F problems the null agent closes, to produce `minif2f.EASY_TAIL`.

Run manually, never from CI:

    uv run python -m lean_agent_eval.suites.survey_minif2f --out /tmp/survey.json

Why this is a separate tool rather than part of the gate, and why the gate's list is checked in:
a gate that recomputed which problems *ought* to close could not fail. A regression that stopped
closing a problem would just shrink the tail and stay green. So the measurement runs here, by hand,
and its result is committed as a fixed expectation.

**It measures the real proof shape, not a lookalike.** The first version of this survey substituted
tactics into upstream's own theorem (`theorem foo (x : T) (h : P) : C := by <tactic>`) and reported
126 of 488 closing. That number is wrong for this system, because sealing does not preserve that
shape: a submitted theorem's *signature* binders become part of the sealed statement
(`∀ (x : T), P → C`), so the tactic faces a `∀` where upstream's faced `C`. This tool therefore
runs `SymbolicPortfolio.development()` itself -- the exact text the policy emits -- against the
exact statement `/v1/decompose` produces. Rehearsing the real thing is the same lesson M2.1.3
recorded when its round-trip check elaborated a bare term instead of the real generated source.

It deliberately needs no Postgres: it measures whether a tactic *elaborates* against the sealed
statement, which is the question that decides the tail. Whether the winner then links, replays,
audits and reaches `proved` is what `tests/eval/test_minif2f.py` establishes against the real
pipeline -- a strictly stronger check on a much smaller set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from lean_agent_core.actions import ObligationContext
from lean_agent_core.executor import SORRY_AXIOM
from lean_agent_policies.symbolic import DEFAULT_TACTICS, SymbolicPortfolio
from lean_agent_serv.repl import ReplCrashed, ReplTimeout, ReplWorker

from lean_agent_eval.suites.minif2f import MiniF2FProblem, load_corpus

#: Long enough for a cold `import Mathlib` in a fresh worker (~30 s measured, with headroom for a
#: loaded machine). Distinct from the per-tactic budget below, which is about a single check.
WARM_TIMEOUT_MS = 600_000

#: Per tactic. `SymbolicPortfolio`'s own default is 10 s; the survey is deliberately no more
#: generous, so a problem that only closes with a longer budget is not written into a tail the
#: gate will then run at 10 s.
TACTIC_TIMEOUT_MS = 10_000

GOAL_DECL = "LeanAgent.Goals.G_1"
ENTRY = "LeanAgent.Sol.sol_1"


@dataclass
class ProblemSurvey:
    id: str
    split: str
    #: Did `/v1/decompose` produce a statement at all? False means the statement does not elaborate
    #: under this repo's toolchain -- upstream targets v4.24.0 and this repo pins v4.33.1.
    decomposed: bool
    #: M2.1.3's check: does the printed statement re-elaborate to the `Expr` it was printed from?
    #: A lemma that does not round-trip is *refused* by ingestion (M2.6) rather than sealed, so it
    #: can never enter the tail however easily a tactic would close it.
    round_trips: bool
    tactic: str | None
    elapsed_ms: int
    diagnostics: tuple[str, ...] = ()


async def _spawn_warm(lake_project_dir: Path, imports: tuple[str, ...]) -> ReplWorker:
    """A worker with its base env actually imported.

    The trivial check is not ceremony: `spawn` returns as soon as the process is up, and for a
    full-Mathlib base env the import itself is ~30 s. Without forcing it here, that cost would be
    charged to whichever problem happened to go first and read as that problem being slow.
    """
    worker = await ReplWorker.spawn(lake_project_dir, imports)
    await worker.check("def warm : Nat := 1", timeout_ms=WARM_TIMEOUT_MS)
    return worker


async def _survey_one(
    worker: ReplWorker,
    problem: MiniF2FProblem,
    opens: str,
    tactics: tuple[str, ...],
    lake_project_dir: Path,
) -> tuple[ProblemSurvey, ReplWorker]:
    """Survey one problem, returning the worker to use next.

    The worker is returned because a `ReplTimeout` SIGKILLs it (M1.8.2), and a dead full-Mathlib
    worker has to be replaced rather than reused -- the caller cannot tell from the result alone.
    """
    started = time.monotonic()
    policy = SymbolicPortfolio(tactics=tactics, tactic_timeout_ms=TACTIC_TIMEOUT_MS)

    async def respawn() -> ReplWorker:
        return await _spawn_warm(lake_project_dir, worker.imports)

    try:
        decomposed = await worker.decompose(f"{opens}\n{problem.as_sorry()}", timeout_ms=120_000)
    except ReplTimeout:
        return ProblemSurvey(problem.id, problem.split, False, False, None, 0, ("timeout",)), (
            await respawn()
        )
    except ReplCrashed as exc:
        return ProblemSurvey(
            problem.id, problem.split, False, False, None, 0, (str(exc),)
        ), await respawn()

    if not decomposed.ok or len(decomposed.lemmas) != 1:
        return ProblemSurvey(
            problem.id,
            problem.split,
            decomposed.ok,
            False,
            None,
            int((time.monotonic() - started) * 1000),
            decomposed.diagnostics,
        ), worker

    lemma = decomposed.lemmas[0]
    if not lemma.round_trips:
        return ProblemSurvey(
            problem.id,
            problem.split,
            True,
            False,
            None,
            int((time.monotonic() - started) * 1000),
            lemma.diagnostics,
        ), worker

    universes = "" if not lemma.level_params else ".{" + ", ".join(lemma.level_params) + "}"
    goal_source = (
        "set_option autoImplicit false\n"
        "namespace LeanAgent.Goals\n"
        f"def G_1{universes} : Sort _ := {lemma.statement}\n"
        "end LeanAgent.Goals\n"
    )
    ctx = ObligationContext(
        obligation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        base_env_digest="",
        bundle_sha="",
        goal_decl=GOAL_DECL,
        goal_src=lemma.statement,
        entry=ENTRY,
        level_params=tuple(lemma.level_params),
    )

    winner: str | None = None
    current = worker
    for tactic in tactics:
        # The goal is declared in the same session rather than imported from a compiled bundle:
        # this measures elaboration, and Link's imported-constant requirement (M1.2) is the
        # pipeline test's business, not the survey's.
        source = goal_source + policy.development(ctx, tactic)
        try:
            result = await current.check(source, timeout_ms=TACTIC_TIMEOUT_MS)
        except ReplTimeout:
            current = await respawn()
            continue
        except ReplCrashed:
            current = await respawn()
            continue
        # `ok` is not enough, and getting this wrong is what the first version of this survey
        # did: a `sorry` is a warning, so `apply?`/`exact?`/`rw?` leaving a partial proof come
        # back `ok=True`. Reading the axiom cone instead is structural -- the same signal the
        # executor screens on and the audit enforces -- rather than a scan for warning text.
        if result.ok and SORRY_AXIOM not in result.axioms:
            winner = tactic
            break

    return ProblemSurvey(
        problem.id,
        problem.split,
        True,
        True,
        winner,
        int((time.monotonic() - started) * 1000),
    ), current


async def survey(
    lake_project_dir: Path,
    *,
    limit: int | None = None,
    tactics: tuple[str, ...] = DEFAULT_TACTICS,
) -> list[ProblemSurvey]:
    corpus = load_corpus()
    problems = corpus.problems[:limit] if limit else corpus.problems

    worker = await _spawn_warm(lake_project_dir, corpus.imports)
    results: list[ProblemSurvey] = []
    try:
        for index, problem in enumerate(problems):
            result, worker = await _survey_one(
                worker, problem, corpus.opens, tactics, lake_project_dir
            )
            results.append(result)
            if result.tactic:
                print(f"  [{index}] {result.split}/{result.id} <- {result.tactic}", flush=True)
            if index % 25 == 0:
                print(f"...{index}/{len(problems)}", flush=True)
    finally:
        await worker.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake-project-dir", default="packages/leankernel", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    results = asyncio.run(survey(args.lake_project_dir.resolve(), limit=args.limit))
    closed = [r for r in results if r.tactic]
    no_seal = [r for r in results if not r.decomposed or not r.round_trips]
    print(f"\nclosed {len(closed)}/{len(results)}; unsealable {len(no_seal)}")
    print("EASY_TAIL candidates (id, tactic, ms):")
    for r in sorted(closed, key=lambda r: r.elapsed_ms):
        print(f"  {r.id!r},  # {r.tactic} {r.elapsed_ms} ms")
    if args.out:
        args.out.write_text(json.dumps([asdict(r) for r in results], indent=1))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
