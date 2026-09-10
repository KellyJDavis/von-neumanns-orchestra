"""Ingestion: submission -> sealed goals -> obligations (spec §6.3 steps 1-4).

    1. **Submit** a `.lean` file, a bare statement, or a `lake` target.
    2. **Resolve base environment.**
    3. **Elaborate once**, extract `sorry` sites (§4.6).
    4. **Seal** each site's goal into a bundle; create one obligation per site.

Steps 5 (prove) and 6 (materialize) are the control loop's and M2.7's. This module is what makes
an obligation exist at all, and everything downstream inherits its decisions -- most importantly
that the goal a policy is given was elaborated *here*, once, before any agent ran.

**No decomposition edges are created.** Spec §6.3's step 4 mentions "edges recording reassembly",
but a submitted file has no parent obligation to hang them from: `CreateRunResponse.root_obligations`
is a list, and each `sorry` site is a root. Reassembling a *file* is materialization (step 6),
which checks the assembled file elaborates and that each declaration links -- not an obligation
DAG edge. Edges appear when a *policy* decomposes an obligation, which is a `Decompose` action the
executor does not perform yet.

A `lake` target is not accepted. Resolving one means running the build system over a project tree,
which is a sandboxing question (spec §7.2: "`lake` never has network access in a serving worker;
dependency resolution happens at image build time") rather than an ingestion one.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from lean_agent_core.digests import compute_goal_digest
from lean_agent_core.protocols import (
    BlobStore,
    DecomposedLemma,
    LeanService,
    SealGoalRequest,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Spec §4.1's namespace, and the prefix `SymbolicPortfolio` strips to derive an entry name.
GOAL_PREFIX = "G_"

#: A goal name must survive `Serve.lean`'s `isValidGoalName` allowlist -- a leading letter or `_`,
#: then letters, digits, `_`, `'`. Names here are *generated*, so this is not defence against a
#: hostile submission (there is nothing to defend: the name never comes from the caller) but
#: against a decomposed lemma name that Lean produced and we would otherwise splice unchanged.
_VALID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")


@dataclass(frozen=True)
class Submission:
    """Spec §6.1's `CreateRunRequest`, minus the transport concerns M2.8 owns.

    Exactly one of `source` (a `.lean` file with `sorry`s) or `statement` (a bare goal) -- they
    are different pipelines: a file is decomposed into its `sorry` sites, a statement becomes one
    obligation directly.
    """

    base_env_digest: str
    tenant_id: uuid.UUID
    source: str | None = None
    statement: str | None = None
    policy: str = "SymbolicPortfolio"
    allow_sorry: bool = False
    axiom_allowlist: tuple[str, ...] = ("propext", "Classical.choice", "Quot.sound")
    max_depth: int = 6
    budget_attempts: int = 8
    budget_tokens: int | None = None
    budget_kernel_ms: int | None = None


@dataclass(frozen=True)
class SealFailure:
    """A goal that did not become an obligation, and why (spec §6.1's `seal_failures`).

    Returned rather than raised: "a submission with ten goals of which one does not elaborate
    creates nine obligations and reports the tenth, instead of failing the whole request."
    """

    name: str
    statement: str
    diagnostics: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class AdmissionReport:
    """Spec §4.5's signals, **roots only** -- "a subgoal that `simp` closes is a good subgoal", so
    running these on decomposition children would flag ordinary progress as mis-formalization.

    Non-blocking by design: recorded on the obligation and reported, never used to reject a
    submission. Two of spec's five signals carry a **[measure]** marker, meaning the threshold
    that would turn a signal into a judgement has not been measured -- so this records *what
    happened* (`closed_by`, `closed_in_ms`) and leaves "is that too fast" to whoever has the data.

    `needs_auto_implicit` is the one signal that is already a judgement rather than a measurement:
    sealing forces `autoImplicit false`, so a statement that only elaborates with it on does not
    seal at all, and re-trying the failure with it on is what distinguishes "mistyped identifier
    silently generalized" from an ordinary elaboration error.

    Two of spec's signals are absent. "Free universe metavariables after elaboration" is not
    separately detectable: M1.1 established it surfaces as an ordinary elaboration error, already
    carried in a seal failure's diagnostics. "No binder is used in the body" needs `Expr`-level
    analysis on the Lean side, which nothing exposes yet.
    """

    closed_by: str | None = None
    closed_in_ms: int | None = None
    needs_auto_implicit: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "closed_by": self.closed_by,
            "closed_in_ms": self.closed_in_ms,
            "needs_auto_implicit": self.needs_auto_implicit,
        }


@dataclass(frozen=True)
class IngestionResult:
    """Spec §6.1's `CreateRunResponse`."""

    run_id: uuid.UUID
    manifest_hash: str
    root_obligations: list[uuid.UUID] = field(default_factory=list)
    admission: dict[uuid.UUID, AdmissionReport] = field(default_factory=dict)
    seal_failures: list[SealFailure] = field(default_factory=list)
    bundle_sha: str | None = None


def build_manifest(submission: Submission, base_env: dict[str, Any]) -> dict[str, Any]:
    """Spec §7.3's run manifest, "frozen at creation and included in every published result".

    Only the sections this system can currently fill honestly. `models` is an empty list because
    Phase 2 makes zero model calls -- not omitted, because an absent key and an empty list say
    different things, and "no models were involved" is exactly the claim a published Phase 2
    result needs to make. `code`/`container` are absent rather than guessed: filling them means
    reading the running container's own image digest and git revision, which is a deployment fact
    M2.8's entry point knows and this function does not.
    """
    return {
        "schema_version": "1.0",
        "lean": {
            "toolchain": base_env.get("toolchain_rev"),
            "mathlib_rev": base_env.get("mathlib_rev"),
        },
        "base_env": {"digest": submission.base_env_digest},
        "models": [],
        "policies": [{"id": submission.policy}],
        "budgets": {
            "attempts": submission.budget_attempts,
            "tokens": submission.budget_tokens,
            "kernel_ms": submission.budget_kernel_ms,
        },
        "axiom_allowlist": list(submission.axiom_allowlist),
        "allow_sorry": submission.allow_sorry,
    }


def canonical_manifest_hash(manifest: dict[str, Any]) -> bytes:
    """`sort_keys` makes the hash a function of the manifest's content rather than of whatever
    order this process happened to build the dict in -- two runs with identical configuration must
    produce identical hashes or the manifest cannot serve as an identity."""
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).digest()


def goal_name(index: int) -> str:
    """`G_1`, `G_2`, ... Generated rather than taken from the submission: the sealed constant's
    name ends up in `LeanAgent.Goals`, and a name a caller chose could collide with another
    submission's or fail `Serve.lean`'s identifier allowlist."""
    return f"{GOAL_PREFIX}{index}"


def _sanitize(lemma: DecomposedLemma, index: int) -> str:
    """A decomposed lemma arrives named `sorry_1` by `Sorries.lean`. Renaming to `G_<n>` keeps the
    sealed namespace uniform and sidesteps any Lean-generated name that would not survive the
    identifier allowlist."""
    del lemma
    return goal_name(index)


class Ingestor:
    """Turns one submission into a run and its root obligations.

    Takes a `LeanService` rather than reaching for leanserv directly: ingestion elaborates,
    decomposes and seals, and every one of those is an operation on a warm worker that belongs
    behind that protocol.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        lean: LeanService,
        blobs: BlobStore,
    ) -> None:
        self._sessions = session_factory
        self._lean = lean
        self._blobs = blobs

    async def ingest(self, submission: Submission) -> IngestionResult:
        if (submission.source is None) == (submission.statement is None):
            raise ValueError("exactly one of `source` or `statement` must be given")

        base_env = await self._resolve_base_env(submission.base_env_digest)
        manifest = build_manifest(submission, base_env)
        manifest_hash = canonical_manifest_hash(manifest)
        run_id = await self._create_run(submission, manifest, manifest_hash)

        candidates, failures = await self._candidates(submission)
        # §4.5's "elaborates only with `autoImplicit true`" is a property of the *submitted
        # development*, not of the abstracted statements it yields, so it has to be measured here
        # -- once, before decomposition erases the evidence. See `_source_needs_auto_implicit`.
        source_needs_auto_implicit = await self._source_needs_auto_implicit(submission)
        if not candidates:
            return IngestionResult(
                run_id=run_id, manifest_hash=manifest_hash.hex(), seal_failures=failures
            )

        sealed = await self._lean.seal(base_env_digest=submission.base_env_digest, goals=candidates)
        bundle_sha = sealed.bundle_digest
        # `put`, not `store_or_inline`: the bundle must be *retrievable by digest* later, and
        # `store_or_inline` deliberately does not store anything under 64 KiB -- it decides how a
        # value is carried in a column, which is a different question. A bundle small enough to
        # inline would simply have vanished, and M2.7 could not compile it. `put`'s returned digest
        # is sha256 of the content, which is exactly `bundle_sha`.
        stored = await self._blobs.put(sealed.bundle_source.encode(), "text/x-lean")
        assert stored.hex() == bundle_sha, "blob store disagreed with /v1/seal about the digest"

        roots: list[uuid.UUID] = []
        admission: dict[uuid.UUID, AdmissionReport] = {}
        for request, goal in zip(candidates, sealed.goals, strict=True):
            if not goal.ok:
                failures.append(
                    SealFailure(
                        name=request.name,
                        statement=request.statement,
                        diagnostics=goal.diagnostics,
                        reason="did not elaborate",
                    )
                )
                continue
            report = await self._admit(submission, request, source_needs_auto_implicit)
            obligation_id = await self._create_obligation(
                run_id=run_id,
                submission=submission,
                goal=goal,
                bundle_sha=bundle_sha,
                admission=report,
            )
            roots.append(obligation_id)
            admission[obligation_id] = report

        return IngestionResult(
            run_id=run_id,
            manifest_hash=manifest_hash.hex(),
            root_obligations=roots,
            admission=admission,
            seal_failures=failures,
            bundle_sha=bundle_sha,
        )

    async def _candidates(
        self, submission: Submission
    ) -> tuple[list[SealGoalRequest], list[SealFailure]]:
        """Spec §6.3 step 3. A bare statement is one candidate; a file is elaborated once and its
        `sorry` sites become the candidates.

        A decomposed lemma whose printed statement does not round-trip is refused here rather than
        sealed: M2.1.3's check says the text does not seal back to the `Expr` it was printed from,
        so an obligation created from it would prove something other than the file needs -- the
        statement drift §1.1 exists to prevent, arriving through the one door that bypasses
        sealing's own guarantee.
        """
        if submission.statement is not None:
            return [SealGoalRequest(name=goal_name(1), statement=submission.statement)], []

        assert submission.source is not None
        decomposed = await self._lean.decompose(
            base_env_digest=submission.base_env_digest, development=submission.source
        )
        if not decomposed.ok:
            return [], [
                SealFailure(
                    name="<submission>",
                    statement=submission.source,
                    diagnostics=decomposed.diagnostics,
                    reason="the submitted file does not elaborate",
                )
            ]

        candidates: list[SealGoalRequest] = []
        failures: list[SealFailure] = []
        for index, lemma in enumerate(decomposed.lemmas, start=1):
            if not lemma.round_trips:
                failures.append(
                    SealFailure(
                        name=lemma.name,
                        statement=lemma.statement,
                        diagnostics=lemma.diagnostics,
                        reason="printed statement does not seal back to the goal it came from",
                    )
                )
                continue
            name = _sanitize(lemma, index)
            if not _VALID_NAME.match(name):  # pragma: no cover - generated names always match
                failures.append(
                    SealFailure(
                        name=lemma.name,
                        statement=lemma.statement,
                        diagnostics=(),
                        reason=f"generated goal name {name!r} is not a plain identifier",
                    )
                )
                continue
            candidates.append(
                SealGoalRequest(
                    name=name, statement=lemma.statement, level_params=lemma.level_params
                )
            )
        return candidates, failures

    async def _source_needs_auto_implicit(self, submission: Submission) -> bool:
        """Spec §4.5: "Elaborates only with `autoImplicit true` -- mistyped identifier silently
        generalized."

        Measured on the submitted development, and this is not a detail. `/v1/decompose` elaborates
        with `autoImplicit` at Lean's default (*on*), unlike `/v1/seal`, which forces it off -- so a
        file containing a typo does not fail: the typo is auto-bound as a binder, `decompose`
        reports success, and the abstracted statement carries it as an honest explicit `∀`. By the
        time a statement reaches sealing there is nothing left to detect, because the generalization
        already happened and is now written down. Found by a test that expected a file containing
        `NoSuchIdentifier` not to elaborate, and got four obligations instead.

        This is the signal working as spec intends rather than a bug to fix: §4.5 calls
        `autoImplicit` firing a *signal*, non-blocking, "recorded on the obligation and reported in
        benchmark output" -- not grounds for rejection. Forcing it off during decomposition would
        instead reject a class of submission spec deliberately admits.
        """
        if submission.source is None:
            return False
        strict = await self._lean.check(
            base_env_digest=submission.base_env_digest,
            body="set_option autoImplicit false\nset_option relaxedAutoImplicit false\n"
            + submission.source,
        )
        return not strict.ok

    async def _admit(
        self, submission: Submission, request: SealGoalRequest, source_needs_auto_implicit: bool
    ) -> AdmissionReport:
        """Spec §4.5's signals, on this root.

        Runs against the *statement*, not the sealed constant, and so needs no materialized bundle
        -- which is what makes admission possible at ingestion time at all, since the `.olean` is
        built lazily out of band afterwards.
        """
        universes = "" if not request.level_params else ".{" + ", ".join(request.level_params) + "}"
        closed_by: str | None = None
        closed_in_ms: int | None = None
        for tactic in ("simp", "decide", "exact?"):
            outcome = await self._lean.check(
                base_env_digest=submission.base_env_digest,
                body=(
                    "set_option linter.defProp false\n"
                    f"example{universes} : {request.statement} := by {tactic}"
                ),
            )
            if outcome.ok:
                closed_by = tactic
                closed_in_ms = outcome.elapsed_ms
                break

        # Sealing forces `autoImplicit false`, so a statement that only elaborates with it on never
        # seals -- re-checking with it on is what tells "mistyped identifier silently generalized"
        # apart from any other elaboration error.
        strict = await self._lean.check(
            base_env_digest=submission.base_env_digest,
            body=(
                "set_option autoImplicit false\nset_option relaxedAutoImplicit false\n"
                f"example{universes} : {request.statement} := by sorry"
            ),
        )
        needs_auto_implicit = source_needs_auto_implicit
        if not needs_auto_implicit and not strict.ok:
            relaxed = await self._lean.check(
                base_env_digest=submission.base_env_digest,
                body=f"set_option autoImplicit true\nexample{universes} : "
                f"{request.statement} := by sorry",
            )
            needs_auto_implicit = relaxed.ok

        return AdmissionReport(
            closed_by=closed_by,
            closed_in_ms=closed_in_ms,
            needs_auto_implicit=needs_auto_implicit,
        )

    async def _resolve_base_env(self, digest_hex: str) -> dict[str, Any]:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text("SELECT toolchain_rev, mathlib_rev FROM base_env WHERE digest = :d"),
                    {"d": bytes.fromhex(digest_hex)},
                )
            ).one_or_none()
        if row is None:
            raise LookupError(f"no base_env with digest {digest_hex!r}")
        return {"toolchain_rev": row[0], "mathlib_rev": row[1]}

    async def _create_run(
        self, submission: Submission, manifest: dict[str, Any], manifest_hash: bytes
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        async with self._sessions() as session:
            await session.execute(
                text(
                    "INSERT INTO run (id, tenant_id, base_env_digest, status, manifest, "
                    "manifest_hash, allow_sorry, axiom_allowlist, max_depth, budget_tokens, "
                    "budget_kernel_ms) VALUES (:id, :tenant, :base_env, 'running', "
                    "CAST(:manifest AS jsonb), :mh, :allow_sorry, :allowlist, :max_depth, "
                    ":tokens, :kernel_ms)"
                ),
                {
                    "id": run_id,
                    "tenant": submission.tenant_id,
                    "base_env": bytes.fromhex(submission.base_env_digest),
                    "manifest": json.dumps(manifest, sort_keys=True),
                    "mh": manifest_hash,
                    "allow_sorry": submission.allow_sorry,
                    "allowlist": list(submission.axiom_allowlist),
                    "max_depth": submission.max_depth,
                    "tokens": submission.budget_tokens,
                    "kernel_ms": submission.budget_kernel_ms,
                },
            )
            await session.commit()
        return run_id

    async def _create_obligation(
        self,
        *,
        run_id: uuid.UUID,
        submission: Submission,
        goal: Any,
        bundle_sha: str,
        admission: AdmissionReport,
    ) -> uuid.UUID:
        """One root obligation.

        `sealed_olean_sha` is left NULL: the bundle's `.olean` is built lazily out of band (spec
        §4.1), and `mark_proved` compares the observed digest against this column -- so a NULL
        here means the obligation genuinely cannot be proved until M2.7 materializes it. That is
        the correct state to be in, not a gap to paper over with a placeholder digest.
        """
        obligation_id = uuid.uuid4()
        async with self._sessions() as session:
            await session.execute(
                text(
                    "INSERT INTO obligation (id, run_id, base_env_digest, goal_digest, "
                    "bundle_sha, goal_src, decl_name, is_root, depth, admission) VALUES "
                    "(:id, :run, :base_env, :gd, :bundle, :src, :decl, true, 0, "
                    "CAST(:admission AS jsonb))"
                ),
                {
                    "id": obligation_id,
                    "run": run_id,
                    "base_env": bytes.fromhex(submission.base_env_digest),
                    "gd": compute_goal_digest(
                        bytes.fromhex(submission.base_env_digest), goal.goal_src
                    ),
                    "bundle": bytes.fromhex(bundle_sha),
                    "src": goal.goal_src,
                    "decl": goal.decl_name,
                    "admission": json.dumps(admission.as_json(), sort_keys=True),
                },
            )
            await session.commit()
        return obligation_id
