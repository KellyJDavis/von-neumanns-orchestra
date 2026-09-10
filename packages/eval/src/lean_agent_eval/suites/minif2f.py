"""miniF2F (spec §7.5's calibration suite, and Phase 2's exit gate).

Spec's Phase 2 exit: "closes the easy tail of miniF2F deterministically in CI at zero token cost,
stable across three runs; a materialized file passes both the elaboration and the link check. This
suite runs on every PR forever and is the only way to later distinguish a broken harness from a
policy that needs tuning."

Every clause of that shapes something here.

**"the easy tail"** is `EASY_TAIL` -- a fixed, checked-in list of problem ids, not a threshold
evaluated at runtime. A gate that recomputed which problems *ought* to be easy could not fail: a
regression that stopped closing a problem would simply redefine the tail and stay green. The list
is what makes this a regression test rather than a measurement, and it was produced by actually
running the portfolio over all 488 problems (`survey.py`).

**"deterministically"** and **"stable across three runs"** are why the gate submits the tail as
*one* multi-`sorry` submission rather than one run per problem. That is also cheaper by a wide
margin, for a reason that is not obvious: `/v1/link` keys its warm worker on `(base_env, bundle)`,
so N separate runs mean N distinct bundles, N distinct pool keys, and -- at ~6 GiB and ~30 s per
full-Mathlib worker -- either N warm-ups or LRU thrashing between them. One submission means one
bundle and one worker.

**"a materialized file passes both the elaboration and the link check"** is singular, and a
multi-`sorry` submission is what gives that check content: it is the whole file, every hole filled,
elaborated in a fresh kernel -- §6.3 step 6's "proves they were filled *compatibly*".

**"zero token cost"** is structural rather than asserted: `SymbolicPortfolio.roles` is empty, so an
executor has nothing to ask a model for. The report still carries the token total, and the gate
still asserts it is zero, because a criterion is worth pinning at the place it would break.

The corpus is vendored, not fetched -- see `vendor_minif2f.py` for why, including the licence and
provenance MIT requires.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from lean_agent_core.enums import VerdictKind

from lean_agent_eval.score import AttemptOutcome, BudgetTotals, SuiteScore, score_suite

CORPUS_PATH = Path(__file__).parent / "data" / "minif2f.json"

#: The submitted file's own header. `import Mathlib` is deliberately *not* part of it: a submission
#: is elaborated as a body against an already-warm base environment, and a body carrying its own
#: `import` line fails outright (M2.7). Those imports live in the base env's recipe instead.
CORPUS_OPENS = "open BigOperators Real Nat Topology Rat"

#: Upstream sets `maxHeartbeats 0` on every problem; this suite deliberately does not carry that
#: into the proof attempts. Unbounded heartbeats mean a tactic that will never succeed runs until
#: the *wallclock* timeout fires, and a wallclock timeout SIGKILLs the worker (M1.8.2) -- which for
#: a full-Mathlib worker costs ~30 s of re-warming to learn nothing. Lean's default heartbeat limit
#: makes such a tactic give up on its own, leaving the worker alive for the next portfolio member.
#: It is a real difference from published harnesses, recorded rather than hidden: it can only lose
#: proofs, never manufacture them.
HEARTBEAT_NOTE = (
    "proof attempts run under Lean's default maxHeartbeats, not upstream's `maxHeartbeats 0`"
)


#: Problems the null agent closes, in a fixed order. Produced by running the portfolio over all
#: 488 problems in the real sealed-goal shape (`survey_minif2f.py`), then narrowing to a slice this
#: gate can afford on every PR forever: every one of `GATE_TACTICS` is represented, both splits
#: appear, and the slowest member closed in about a second during the survey.
#:
#: Checked in rather than recomputed, which is the whole point: a gate that worked out for itself
#: which problems ought to be easy could not fail -- a regression that stopped closing one would
#: quietly redefine the tail and stay green. Each entry earns its place by closing through the
#: *whole* acceptance path (seal, link, replay, audit, `mark_proved`), which
#: `tests/eval/test_minif2f.py` establishes and which is strictly stronger than the survey's own
#: "a tactic elaborated" screen.
EASY_TAIL: tuple[str, ...] = (
    "amc12b_2002_p2",  # simp_all, 178 ms, test
    "mathd_algebra_188",  # simp_all, 167 ms, test
    "mathd_algebra_24",  # linarith, 667 ms, test
    "mathd_algebra_304",  # rfl, 126 ms, test
    "mathd_algebra_314",  # simp, 183 ms, test
    "mathd_algebra_359",  # linarith, 686 ms, test
    "mathd_algebra_478",  # nlinarith, 1008 ms, test
    "mathd_algebra_536",  # norm_num, 235 ms, valid
    "mathd_numbertheory_136",  # omega, 171 ms, valid
    "mathd_numbertheory_198",  # omega, 179 ms, valid
    "mathd_numbertheory_252",  # rfl, 113 ms, valid
    "mathd_numbertheory_64",  # decide, 139 ms, valid
    "mathd_numbertheory_66",  # rfl, 117 ms, test
)

#: Everything the survey closed: 114 of 488 (23.4%), with 57 more that decompose but whose printed
#: statement does not round-trip, so ingestion refuses to seal them (M2.6). Not asserted by the
#: gate -- kept beside it so a later change can be compared against what was actually measured
#: rather than against the CI slice. Notably every one of the 488 statements *does* still
#: elaborate under this repo's v4.33.1 despite upstream targeting v4.24.0: the toolchain gap costs
#: nothing at elaboration, and the 57 losses are a pretty-printing round-trip problem instead.
MEASURED_TAIL: tuple[str, ...] = (
    "aime_1989_p8",  # linarith
    "amc12_2001_p2",  # nlinarith
    "amc12a_2008_p2",  # linarith
    "amc12a_2021_p9",  # rfl
    "amc12b_2002_p19",  # nlinarith
    "amc12b_2002_p2",  # simp_all
    "mathd_algebra_104",  # linarith
    "mathd_algebra_107",  # linarith
    "mathd_algebra_109",  # linarith
    "mathd_algebra_119",  # linarith
    "mathd_algebra_123",  # omega
    "mathd_algebra_141",  # nlinarith
    "mathd_algebra_142",  # linarith
    "mathd_algebra_15",  # simp_all
    "mathd_algebra_160",  # linarith
    "mathd_algebra_176",  # linarith
    "mathd_algebra_188",  # simp_all
    "mathd_algebra_24",  # linarith
    "mathd_algebra_289",  # aesop
    "mathd_algebra_304",  # rfl
    "mathd_algebra_314",  # simp
    "mathd_algebra_329",  # linarith
    "mathd_algebra_354",  # linarith
    "mathd_algebra_359",  # linarith
    "mathd_algebra_37",  # nlinarith
    "mathd_algebra_388",  # linarith
    "mathd_algebra_398",  # linarith
    "mathd_algebra_400",  # linarith
    "mathd_algebra_412",  # linarith
    "mathd_algebra_419",  # nlinarith
    "mathd_algebra_427",  # linarith
    "mathd_algebra_432",  # linarith
    "mathd_algebra_440",  # linarith
    "mathd_algebra_455",  # linarith
    "mathd_algebra_478",  # nlinarith
    "mathd_algebra_51",  # linarith
    "mathd_algebra_536",  # norm_num
    "mathd_algebra_568",  # linarith
    "mathd_algebra_616",  # simp_all
    "mathd_algebra_96",  # linarith
    "mathd_numbertheory_101",  # rfl
    "mathd_numbertheory_102",  # rfl
    "mathd_numbertheory_109",  # aesop
    "mathd_numbertheory_110",  # omega
    "mathd_numbertheory_1124",  # omega
    "mathd_numbertheory_12",  # rfl
    "mathd_numbertheory_127",  # rfl
    "mathd_numbertheory_132",  # rfl
    "mathd_numbertheory_135",  # aesop
    "mathd_numbertheory_136",  # omega
    "mathd_numbertheory_149",  # rfl
    "mathd_numbertheory_155",  # rfl
    "mathd_numbertheory_169",  # rfl
    "mathd_numbertheory_175",  # omega
    "mathd_numbertheory_185",  # omega
    "mathd_numbertheory_188",  # rfl
    "mathd_numbertheory_198",  # omega
    "mathd_numbertheory_200",  # rfl
    "mathd_numbertheory_202",  # rfl
    "mathd_numbertheory_207",  # rfl
    "mathd_numbertheory_212",  # rfl
    "mathd_numbertheory_229",  # rfl
    "mathd_numbertheory_235",  # rfl
    "mathd_numbertheory_236",  # omega
    "mathd_numbertheory_237",  # rfl
    "mathd_numbertheory_239",  # rfl
    "mathd_numbertheory_24",  # rfl
    "mathd_numbertheory_247",  # omega
    "mathd_numbertheory_252",  # rfl
    "mathd_numbertheory_254",  # rfl
    "mathd_numbertheory_269",  # rfl
    "mathd_numbertheory_284",  # omega
    "mathd_numbertheory_293",  # omega
    "mathd_numbertheory_299",  # rfl
    "mathd_numbertheory_3",  # rfl
    "mathd_numbertheory_30",  # rfl
    "mathd_numbertheory_301",  # omega
    "mathd_numbertheory_320",  # omega
    "mathd_numbertheory_328",  # omega
    "mathd_numbertheory_33",  # omega
    "mathd_numbertheory_335",  # omega
    "mathd_numbertheory_34",  # omega
    "mathd_numbertheory_341",  # aesop
    "mathd_numbertheory_342",  # rfl
    "mathd_numbertheory_343",  # rfl
    "mathd_numbertheory_345",  # rfl
    "mathd_numbertheory_37",  # rfl
    "mathd_numbertheory_370",  # omega
    "mathd_numbertheory_403",  # rfl
    "mathd_numbertheory_447",  # rfl
    "mathd_numbertheory_45",  # rfl
    "mathd_numbertheory_458",  # omega
    "mathd_numbertheory_461",  # rw?
    "mathd_numbertheory_466",  # rfl
    "mathd_numbertheory_48",  # nlinarith
    "mathd_numbertheory_483",  # simp_all
    "mathd_numbertheory_517",  # rfl
    "mathd_numbertheory_551",  # rfl
    "mathd_numbertheory_559",  # omega
    "mathd_numbertheory_582",  # omega
    "mathd_numbertheory_629",  # decide
    "mathd_numbertheory_64",  # decide
    "mathd_numbertheory_640",  # rfl
    "mathd_numbertheory_66",  # rfl
    "mathd_numbertheory_728",  # rfl
    "mathd_numbertheory_739",  # rfl
    "mathd_numbertheory_765",  # omega
    "mathd_numbertheory_769",  # rfl
    "mathd_numbertheory_81",  # rfl
    "mathd_numbertheory_85",  # rfl
    "mathd_numbertheory_92",  # omega
    "mathd_numbertheory_961",  # rfl
    "mathd_numbertheory_99",  # omega
    "numbertheory_2pownm1prime_nprime",  # exact?
)


@dataclass(frozen=True)
class MiniF2FProblem:
    id: str
    split: str
    #: Upstream's theorem, ending at `:= by` -- the vendorer strips the `sorry` so a consumer
    #: splices at exactly the placeholder's position rather than reconstructing the syntax.
    statement: str
    statement_sha256: str

    def as_sorry(self) -> str:
        """The problem as a submittable development: upstream's statement, `sorry`'d."""
        return f"{self.statement} sorry"


@dataclass(frozen=True)
class MiniF2FCorpus:
    problems: tuple[MiniF2FProblem, ...]
    provenance: dict[str, Any]
    imports: tuple[str, ...]
    opens: str
    corpus_sha256: str

    def by_id(self, ids: Sequence[str]) -> tuple[MiniF2FProblem, ...]:
        """Look problems up in the order asked for, raising on an id the corpus does not have.

        Raising matters: `EASY_TAIL` is checked in beside the corpus, and an id that silently
        vanished (an upstream rename, a careless re-pin) would otherwise shrink the gate's own
        expectations with nothing failing.
        """
        index = {problem.id: problem for problem in self.problems}
        missing = [i for i in ids if i not in index]
        if missing:
            raise KeyError(f"corpus has no problem(s) {missing!r}")
        return tuple(index[i] for i in ids)


def load_corpus(path: Path = CORPUS_PATH) -> MiniF2FCorpus:
    data = json.loads(path.read_text())
    return MiniF2FCorpus(
        problems=tuple(
            MiniF2FProblem(
                id=p["id"],
                split=p["split"],
                statement=p["statement"],
                statement_sha256=p["statement_sha256"],
            )
            for p in data["problems"]
        ),
        provenance=dict(data["provenance"]),
        imports=tuple(data["header"]["imports"]),
        opens=data["header"]["opens"],
        corpus_sha256=data["corpus_sha256"],
    )


def build_submission_source(
    problems: Sequence[MiniF2FProblem], *, opens: str = CORPUS_OPENS
) -> str:
    """The given problems as one multi-`sorry` development.

    One submission rather than one per problem, for the warm-worker reason in this module's
    docstring -- and because spec's exit criterion says "*a* materialized file", which only says
    something about compatible hole-filling when the file has more than one hole.
    """
    parts = [opens, ""]
    for problem in problems:
        parts.extend((problem.as_sorry(), ""))
    return "\n".join(parts).rstrip() + "\n"


@dataclass(frozen=True)
class IngestedSubmission:
    """What the pipeline's ingest step reports back.

    Keyed by *goal index* rather than positionally, because that is the alignment the system
    actually guarantees: ingestion names goals `G_1`, `G_2`, ... in submission order, and a goal
    that fails to seal produces no obligation at all -- spec §6.1's "a submission with ten goals of
    which one does not elaborate creates nine obligations and reports the tenth". Returning a
    ten-long list with a hole in it would require the pipeline to know how many problems were
    submitted; returning the mapping lets `run_suite` do that alignment where the problem list
    actually lives, with nothing guessed.
    """

    run_id: uuid.UUID
    #: 1-based goal index (the `n` in `G_n`) to the obligation it became.
    obligation_by_goal: dict[int, uuid.UUID]
    #: Goal name (`G_n`) to the diagnostics explaining why it did not seal.
    seal_failures: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class ArtifactResult:
    """Spec §6.3 step 6's file and its two checks, as the gate reads them."""

    source: str
    complete: bool
    elaborates: bool
    links: bool
    holes: int
    unfilled: tuple[str, ...]


class MiniF2FPipeline(Protocol):
    """The Phase 2 pipeline this suite drives.

    A protocol rather than a concrete class because assembling the real thing needs a live
    Postgres, a `leanserv` app and a Lake project, all of which the caller already owns and none of
    which belongs to scoring. `tests/eval/test_minif2f.py` supplies the real implementation, built
    from `Ingestor`, `BundleMaterializer`, `FileMaterializer` and the real
    `lean_agent_core.worker.Worker` -- the same components a deployment runs.
    """

    async def ingest(self, source: str) -> IngestedSubmission: ...

    async def materialize(self, run_id: uuid.UUID) -> None: ...

    async def drain(self) -> None:
        """Run the control loop until it has no more claimable work."""
        ...

    async def attempt_outcomes(
        self, run_id: uuid.UUID
    ) -> dict[uuid.UUID, tuple[AttemptOutcome, ...]]: ...

    async def winning_tactics(self, run_id: uuid.UUID) -> dict[uuid.UUID, str]: ...

    async def artifact(self, run_id: uuid.UUID) -> ArtifactResult: ...


@dataclass(frozen=True)
class ProblemResult:
    """One problem's trip through the whole pipeline.

    `sealed` and `proved` are separate on purpose. A statement written for Lean v4.24 that no
    longer elaborates under this repo's v4.33.1 fails to *seal*, which says something about the
    corpus; a statement that seals and is not proved says something about the policy. Collapsing
    them would report a toolchain mismatch as a capability result -- the same confusion
    `infra_error` exists to prevent one layer down.
    """

    id: str
    sealed: bool
    proved: bool
    outcomes: tuple[AttemptOutcome, ...]
    tactic: str | None
    diagnostics: tuple[str, ...] = ()

    @property
    def infra_error(self) -> bool:
        return any(o.kind is VerdictKind.INFRA_ERROR or o.kind is None for o in self.outcomes)


@dataclass(frozen=True)
class SuiteReport:
    """Spec §7.5's scoring discipline: "report pass@k **with the budget that produced it**
    (tokens, kernel-seconds, wallclock, sample count); report the `infra_error` rate alongside"."""

    corpus_sha256: str
    provenance: dict[str, Any]
    results: tuple[ProblemResult, ...]
    artifact: ArtifactResult
    score: SuiteScore
    wallclock_ms: int
    notes: tuple[str, ...] = (HEARTBEAT_NOTE,)

    @property
    def proved(self) -> frozenset[str]:
        return frozenset(r.id for r in self.results if r.proved)

    @property
    def unsealed(self) -> frozenset[str]:
        return frozenset(r.id for r in self.results if not r.sealed)

    @property
    def infra_errors(self) -> frozenset[str]:
        return frozenset(r.id for r in self.results if r.infra_error)

    @property
    def budget(self) -> BudgetTotals:
        return self.score.total_budget

    @property
    def tokens(self) -> int:
        return self.score.total_budget.tokens

    def summary(self) -> str:
        pass_at_1 = self.score.mean_pass_at_k.get(1, 0.0)
        return (
            f"miniF2F[{self.corpus_sha256[:12]}] proved {len(self.proved)}/{len(self.results)} "
            f"(mean pass@1={pass_at_1:.3f}), unsealed {len(self.unsealed)}, "
            f"infra_error rate {self.score.overall_infra_error_rate:.3f}, "
            f"tokens {self.tokens}, kernel {self.budget.kernel_ms} ms, "
            f"wallclock {self.wallclock_ms} ms; artifact: complete={self.artifact.complete} "
            f"elaborates={self.artifact.elaborates} links={self.artifact.links} "
            f"holes={self.artifact.holes}"
        )


async def run_suite(
    pipeline: MiniF2FPipeline,
    problems: Sequence[MiniF2FProblem],
    *,
    corpus: MiniF2FCorpus,
    ks: Sequence[int] = (1,),
) -> SuiteReport:
    """Drive `problems` through the whole Phase 2 pipeline and report.

    Ingest, materialize, drain the control loop, read the artifact -- in that order, and the order
    is the memory plan as much as the logic: ingestion's warm worker is keyed on the base env alone
    and is idle by the time linking wants its own `(base_env, bundle)` worker, so a pool capped at
    one total worker evicts rather than holding two full-Mathlib processes at once.
    """
    started = time.monotonic()

    ingested = await pipeline.ingest(build_submission_source(problems, opens=corpus.opens))
    await pipeline.materialize(ingested.run_id)
    await pipeline.drain()

    outcomes_by_obligation = await pipeline.attempt_outcomes(ingested.run_id)
    tactics = await pipeline.winning_tactics(ingested.run_id)
    artifact = await pipeline.artifact(ingested.run_id)

    results: list[ProblemResult] = []
    for index, problem in enumerate(problems, start=1):
        obligation_id = ingested.obligation_by_goal.get(index)
        if obligation_id is None:
            results.append(
                ProblemResult(
                    id=problem.id,
                    sealed=False,
                    proved=False,
                    outcomes=(),
                    tactic=None,
                    diagnostics=ingested.seal_failures.get(f"G_{index}", ()),
                )
            )
            continue
        outcomes = outcomes_by_obligation.get(obligation_id, ())
        results.append(
            ProblemResult(
                id=problem.id,
                sealed=True,
                proved=any(o.kind is VerdictKind.PROVED for o in outcomes),
                outcomes=outcomes,
                tactic=tactics.get(obligation_id),
            )
        )

    # An unsealed problem contributes no attempts, so it is absent from the scoring input rather
    # than present with zero: no policy ever saw it, and counting it as a miss would report a
    # corpus/toolchain mismatch as a capability result.
    scored = {r.id: r.outcomes for r in results if r.sealed}
    return SuiteReport(
        corpus_sha256=corpus.corpus_sha256,
        provenance=corpus.provenance,
        results=tuple(results),
        artifact=artifact,
        score=score_suite(scored, ks),
        wallclock_ms=int((time.monotonic() - started) * 1000),
    )


def _cli() -> int:  # pragma: no cover - manual entry point
    """Print the corpus's provenance without needing any infrastructure.

    Useful on its own: the upstream toolchain recorded here is the first thing to check when a
    statement stops sealing after a bump (spec §7.6's "seal broke" vs "proof broke").
    """
    corpus = load_corpus()
    print(f"{len(corpus.problems)} problems, corpus_sha256={corpus.corpus_sha256}")
    print(json.dumps(corpus.provenance, indent=1))
    print(f"easy tail: {len(EASY_TAIL)} of them")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
