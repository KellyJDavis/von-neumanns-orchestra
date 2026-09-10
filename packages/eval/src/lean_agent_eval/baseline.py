"""The recorded Phase 2 symbolic baseline, and how a later phase is held to it.

Phase 3's exit criterion (spec §8) opens with **"the Phase 2 symbolic baseline still passes
bit-identically"**, and that clause is the reason this module exists rather than a nice-to-have.
A model policy is supposed to *dominate* the symbolic one; if wiring a model in also quietly
perturbs what the symbolic path produces, the comparison it is being judged against has moved and
the domination claim means nothing.

**"Bit-identically" is interpreted as: the same problems seal to the same statements, the same
tactic wins each one, the accepted proof text is byte-identical, its axiom cone is unchanged, and
the materialized artifact is byte-identical.** Deliberately *not* included: uuids, timestamps and
any elapsed/kernel millisecond count. Those differ run to run for reasons that say nothing about
behaviour, and a baseline that failed on them would be re-recorded so often it would stop being
evidence of anything.

**The record has to exist before the change it is protecting against.** A baseline captured after
Phase 3 starts would faithfully record whatever Phase 3 had already broken. That is the whole
reason this is M3.0 and not M3.11.

Re-recording is deliberate and manual (see `RERECORD_ENV`), never automatic. A legitimate change
to the portfolio *should* move this file -- what must not happen is it moving without anyone
looking, which is exactly what an auto-refreshing golden file guarantees.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASELINE_PATH = Path(__file__).parent / "suites" / "data" / "phase2_baseline.json"

#: Set to `1` to rewrite the baseline instead of asserting against it. An environment variable
#: rather than a flag on the gate: re-recording is a thing a person does on purpose, having read
#: the diff, and it should look nothing like an ordinary test run.
RERECORD_ENV = "LEAN_AGENT_RERECORD_BASELINE"

#: The one run-specific line in a materialized artifact (`-- run: <uuid>`). Normalized out before
#: digesting, so the artifact digest tracks the *content* rather than which run emitted it.
_RUN_LINE = re.compile(r"^-- run: [0-9a-fA-F-]{36}$", re.MULTILINE)


def normalize_artifact(source: str) -> str:
    return _RUN_LINE.sub("-- run: <normalized>", source)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class ProblemBaseline:
    """What one problem produced, reduced to the parts that must not drift.

    `proof_sha256` rather than the proof text itself: the text is already in the repository's
    reach through the corpus and the policy, and a digest makes a drift *loud* -- a diff of two
    tactic scripts invites reading past it, a changed digest does not.
    """

    id: str
    sealed: bool
    proved: bool
    tactic: str | None
    goal_src_sha256: str | None
    proof_sha256: str | None
    axioms: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sealed": self.sealed,
            "proved": self.proved,
            "tactic": self.tactic,
            "goal_src_sha256": self.goal_src_sha256,
            "proof_sha256": self.proof_sha256,
            "axioms": list(self.axioms),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ProblemBaseline:
        return cls(
            id=data["id"],
            sealed=data["sealed"],
            proved=data["proved"],
            tactic=data["tactic"],
            goal_src_sha256=data["goal_src_sha256"],
            proof_sha256=data["proof_sha256"],
            axioms=tuple(data["axioms"]),
        )


@dataclass(frozen=True)
class Phase2Baseline:
    """The whole recorded baseline.

    `policy_config_hash` is in here because a portfolio change *should* invalidate everything
    below it: `SymbolicPortfolio.config_hash` covers the tactic list in order and the per-tactic
    timeout, so a changed hash is a changed experiment, not a regression. Recording it makes the
    difference between those two readings explicit instead of leaving it to whoever reads the
    failure.
    """

    corpus_sha256: str
    policy_id: str
    policy_config_hash: str
    tactics: tuple[str, ...]
    problems: tuple[ProblemBaseline, ...]
    artifact_sha256: str
    artifact_holes: int

    def as_json(self) -> dict[str, Any]:
        return {
            "corpus_sha256": self.corpus_sha256,
            "policy_id": self.policy_id,
            "policy_config_hash": self.policy_config_hash,
            "tactics": list(self.tactics),
            "artifact_sha256": self.artifact_sha256,
            "artifact_holes": self.artifact_holes,
            "problems": [p.as_json() for p in self.problems],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Phase2Baseline:
        return cls(
            corpus_sha256=data["corpus_sha256"],
            policy_id=data["policy_id"],
            policy_config_hash=data["policy_config_hash"],
            tactics=tuple(data["tactics"]),
            problems=tuple(ProblemBaseline.from_json(p) for p in data["problems"]),
            artifact_sha256=data["artifact_sha256"],
            artifact_holes=data["artifact_holes"],
        )

    def by_id(self) -> dict[str, ProblemBaseline]:
        return {p.id: p for p in self.problems}


def load(path: Path = BASELINE_PATH) -> Phase2Baseline:
    return Phase2Baseline.from_json(json.loads(path.read_text()))


def save(baseline: Phase2Baseline, path: Path = BASELINE_PATH) -> None:
    path.write_text(json.dumps(baseline.as_json(), indent=1) + "\n")


def compare(recorded: Phase2Baseline, current: Phase2Baseline) -> tuple[str, ...]:
    """Every way `current` differs from `recorded`, in readable form.

    Returns differences rather than raising, and describes each one rather than reporting
    inequality, because the failure this exists to catch is somebody's *unrelated* change moving
    the symbolic path -- and "baselines differ" would send them looking in the wrong place. Which
    field moved is most of the diagnosis: a changed `goal_src_sha256` is a sealing or
    pretty-printing change, a changed `proof_sha256` with the same tactic is a policy-text change,
    a changed axiom cone is an audit-surface change.
    """
    diffs: list[str] = []

    if recorded.policy_config_hash != current.policy_config_hash:
        diffs.append(
            f"policy config changed: {recorded.policy_id} "
            f"{recorded.policy_config_hash[:12]} -> {current.policy_config_hash[:12]} "
            f"(tactics {list(recorded.tactics)} -> {list(current.tactics)}). "
            "This invalidates the whole baseline by design; re-record deliberately."
        )
    if recorded.corpus_sha256 != current.corpus_sha256:
        diffs.append(
            f"corpus changed: {recorded.corpus_sha256[:12]} -> {current.corpus_sha256[:12]} "
            "-- the benchmark itself moved, so nothing below is comparable."
        )

    old, new = recorded.by_id(), current.by_id()
    for missing in sorted(set(old) - set(new)):
        diffs.append(f"{missing}: was in the baseline and is absent now")
    for added in sorted(set(new) - set(old)):
        diffs.append(f"{added}: is present now and was not in the baseline")

    for pid in sorted(set(old) & set(new)):
        a, b = old[pid], new[pid]
        if a.sealed != b.sealed:
            diffs.append(f"{pid}: sealed {a.sealed} -> {b.sealed}")
        if a.goal_src_sha256 != b.goal_src_sha256:
            diffs.append(
                f"{pid}: sealed statement changed ({a.goal_src_sha256} -> {b.goal_src_sha256}) "
                "-- decomposition or pretty-printing moved, so this is a different goal"
            )
        if a.proved != b.proved:
            diffs.append(f"{pid}: proved {a.proved} -> {b.proved}")
        if a.tactic != b.tactic:
            diffs.append(f"{pid}: winning tactic {a.tactic} -> {b.tactic}")
        if a.proof_sha256 != b.proof_sha256:
            diffs.append(
                f"{pid}: accepted proof text changed ({a.proof_sha256} -> {b.proof_sha256})"
            )
        if a.axioms != b.axioms:
            diffs.append(f"{pid}: axiom cone {list(a.axioms)} -> {list(b.axioms)}")

    if recorded.artifact_holes != current.artifact_holes:
        diffs.append(f"artifact holes {recorded.artifact_holes} -> {current.artifact_holes}")
    if recorded.artifact_sha256 != current.artifact_sha256:
        diffs.append(
            f"materialized artifact changed ({recorded.artifact_sha256[:12]} -> "
            f"{current.artifact_sha256[:12]}) -- the file a user takes away is not the same file"
        )
    return tuple(diffs)


@dataclass(frozen=True)
class AcceptedProof:
    """The accepted evidence for one obligation, as the baseline needs it.

    Read from the `verdict` row rather than reconstructed: that row is what `mark_proved` actually
    checked the §1.1 predicate against, so it is the only account of the proof that cannot
    disagree with the one the system acted on.
    """

    obligation_id: uuid.UUID
    goal_src: str
    proof_text: str | None
    axioms: tuple[str, ...]


def record(
    report: Any,
    *,
    policy_id: str,
    policy_config_hash: bytes,
    tactics: tuple[str, ...],
) -> Phase2Baseline:
    """Reduce a `SuiteReport` to the baseline.

    Takes the report structurally rather than importing `SuiteReport`, only because
    `suites.minif2f` imports `AcceptedProof` from here and the other direction would be a cycle.
    The policy identity is passed in rather than read off the report: the report describes what
    happened, and *which configuration produced it* is the caller's knowledge.
    """
    problems = tuple(
        ProblemBaseline(
            id=r.id,
            sealed=r.sealed,
            proved=r.proved,
            tactic=r.tactic,
            goal_src_sha256=sha256_text(r.goal_src) if r.goal_src is not None else None,
            proof_sha256=sha256_text(r.proof_text) if r.proof_text is not None else None,
            axioms=tuple(r.axioms),
        )
        for r in report.results
    )
    return Phase2Baseline(
        corpus_sha256=report.corpus_sha256,
        policy_id=policy_id,
        policy_config_hash=policy_config_hash.hex(),
        tactics=tactics,
        problems=problems,
        artifact_sha256=sha256_text(normalize_artifact(report.artifact.source)),
        artifact_holes=report.artifact.holes,
    )
