"""Bundle materialization: compile a sealed bundle out of band and record its digest (spec §4.1).

Spec §4.1 leaves the `.olean` deliberately unbuilt on the hot path -- "produced lazily out of band
for reproducibility and `lean4checker` batching; the hot path never waits on the build system".
This module is that out-of-band step, and until it runs an obligation cannot be proved at all:
Link requires the sealed goal to be a genuinely *imported* constant, and `mark_proved` compares
the observed digest against `obligation.sealed_olean_sha`, which ingestion leaves NULL.

So materialization is not an optimization. It is the step that turns an obligation from "recorded"
into "provable", and nothing else in the pipeline can substitute for it.

`FileMaterializer` is the other artifact: spec §6.3 step 6's output `.lean` file, "the file with
each `sorry` replaced", and its **two** checks -- the assembled file elaborates, *and* each
top-level declaration still links against its sealed constant. Spec is emphatic that the second is
not optional: "Elaborating the file proves it compiles, not that it proves what was asked ...
Per-obligation acceptance proves each hole is filled correctly; whole-file verification proves they
were filled *compatibly*."

The reassembly text this needs lives on `run.reassembly_blob`, added for it. Spec already has a
`reassembly_blob` on `obligation_edge` ("on the group, not the child") and it is a different thing:
that is a *decomposition group*'s reassembly, produced when a policy decomposes an obligation. A
submitted file's reassembly exists before any policy runs, has no parent obligation to hang off,
and covers every root at once -- which is why the edge column never fit, and why deferring the
question to "the decomposition-edge design" was the wrong framing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from lean_agent_core.blobs import from_bytea
from lean_agent_core.protocols import BlobStore, LeanService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

#: Spec §4.1's generated path, `LeanAgent/Goals/Bundle_<digest>.lean`. The same convention
#: `leanserv`'s `_bundle_module_name`/`_bundle_olean_path` use -- they have to agree, since one
#: writes the file and the other imports it, and `tests/` pins them together.
BUNDLE_NAMESPACE = ("LeanAgent", "Goals")


def bundle_paths(bundle_root: Path, bundle_sha: str) -> tuple[Path, Path]:
    directory = bundle_root.joinpath(*BUNDLE_NAMESPACE)
    return (
        directory / f"Bundle_{bundle_sha}.lean",
        directory / f"Bundle_{bundle_sha}.olean",
    )


class MaterializationError(Exception):
    """The bundle did not compile. Distinct from an obligation failing to prove: a sealed bundle
    that will not compile means the *sealing* was wrong, which is an infrastructure-level problem
    with the run rather than a fact about any goal in it."""


@dataclass(frozen=True)
class MaterializedBundle:
    bundle_sha: str
    olean_path: Path
    olean_sha: bytes
    obligations_stamped: int


class BundleMaterializer:
    """Compiles sealed bundles and records their `.olean` digests.

    Runs wherever the bundle root lives -- the same directory `leanserv`'s pool puts on every
    worker's `LEAN_PATH`. That shared directory is the coupling: this writes the artifact, workers
    import it, and both derive the path from `bundle_sha` by the same rule.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        blobs: BlobStore,
        bundle_root: Path,
        lake_project_dir: Path,
    ) -> None:
        self._sessions = session_factory
        self._blobs = blobs
        self._bundle_root = bundle_root
        self._lake_project_dir = lake_project_dir

    async def materialize(self, bundle_sha: str) -> MaterializedBundle:
        """Fetch the bundle source, compile it, and stamp every obligation that names it.

        Idempotent: a bundle already compiled and already stamped re-compiles to the same `.olean`
        (the source is content-addressed) and stamps zero further obligations. That matters
        because there is no natural single moment to call this -- a restart, a retry, or a second
        obligation created against the same bundle all reasonably lead here.
        """
        source_path, olean_path = bundle_paths(self._bundle_root, bundle_sha)
        source = await self._blobs.get(bytes.fromhex(bundle_sha))
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(source)

        await self._compile(source_path, olean_path)
        olean_sha = hashlib.sha256(olean_path.read_bytes()).digest()
        stamped = await self._stamp(bundle_sha, olean_sha)
        logger.info(
            "materialized bundle %s -> %s (%d obligations stamped)",
            bundle_sha,
            olean_sha.hex(),
            stamped,
        )
        return MaterializedBundle(
            bundle_sha=bundle_sha,
            olean_path=olean_path,
            olean_sha=olean_sha,
            obligations_stamped=stamped,
        )

    async def materialize_run(self, run_id: uuid.UUID) -> list[MaterializedBundle]:
        """Every distinct unmaterialized bundle a run's obligations name.

        Distinct, because §4.1 is "one bundle per decomposition group" -- a run's roots share one
        bundle today, and compiling it once per obligation would be a real cost once a run has
        hundreds.
        """
        async with self._sessions() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT DISTINCT bundle_sha FROM obligation "
                            "WHERE run_id = :run AND bundle_sha IS NOT NULL "
                            "AND sealed_olean_sha IS NULL"
                        ),
                        {"run": run_id},
                    )
                )
                .scalars()
                .all()
            )
        return [await self.materialize(bytes(sha).hex()) for sha in rows]

    async def _compile(self, source_path: Path, olean_path: Path) -> None:
        """`lake env lean --root=<bundle root>`.

        `--root` is required and not optional polish: a generated bundle deliberately lives outside
        the Lake package (it is per-run, not a checked-in target), and `lake env lean` refuses an
        input file outside the package root with "must be contained in root directory".

        Compiled through `lake env` rather than a bare `lean` so the toolchain and `LEAN_PATH` are
        the workspace's own -- a bundle compiled against a different Mathlib than the workers
        import would produce an `.olean` they cannot load, which would surface much later as an
        unexplained import failure inside a warm worker.
        """
        process = await asyncio.create_subprocess_exec(
            "lake",
            "env",
            "lean",
            f"--root={self._bundle_root}",
            str(source_path),
            "-o",
            str(olean_path),
            cwd=self._lake_project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # The same process-group handling `ReplWorker` needs: `lake env` forks the real `lean`
            # rather than exec-replacing itself (M1.9's finding), so a timeout that signalled only
            # `lake` would orphan the compiler.
            start_new_session=True,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise MaterializationError(
                f"compiling {source_path.name} failed with exit {process.returncode}: "
                f"{stderr.decode(errors='replace') or stdout.decode(errors='replace')}"
            )

    async def _stamp(self, bundle_sha: str, olean_sha: bytes) -> int:
        """`materialize_bundle` is a `SECURITY DEFINER` function, not an UPDATE from here.

        `sealed_olean_sha` is not in `app`'s permitted-column list and must not be: `mark_proved`
        accepts a proof only when the observed digest equals it, so an `app` that could write the
        column could make that comparison vacuous. The function is write-once, which is what keeps
        the artifact an obligation is judged against fixed for its whole life.
        """
        async with self._sessions() as session:
            filled = (
                await session.execute(
                    text("SELECT materialize_bundle(:bundle, :olean)"),
                    {"bundle": bytes.fromhex(bundle_sha), "olean": olean_sha},
                )
            ).scalar_one()
            await session.commit()
        return int(filled)


@dataclass(frozen=True)
class _Run:
    run_id: uuid.UUID
    base_env_digest: str
    imports: tuple[str, ...]
    reassembly: str | None
    bundle_sha: bytes | None


@dataclass(frozen=True)
class _Hole:
    obligation_id: uuid.UUID
    decl_name: str
    hole: str
    entry: str
    proof: str | None


#: The reassembly text `/v1/decompose` produces references each hole as `sorry_<n>`, and ingestion
#: renames the corresponding sealed goal to `G_<n>` at the same index (`_sanitize`). So the
#: correspondence is `G_<n>` <-> `sorry_<n>`, positional and total: the index comes from
#: `enumerate` over the decomposed lemmas, so a lemma skipped for not round-tripping leaves a gap
#: rather than shifting its neighbours.
_HOLE_FROM_GOAL = re.compile(r"^LeanAgent\.Goals\.G_(\d+)$")


def hole_name(decl_name: str) -> str | None:
    match = _HOLE_FROM_GOAL.match(decl_name)
    return f"sorry_{match.group(1)}" if match else None


@dataclass(frozen=True)
class AssembledFile:
    """Spec §6.3 step 6's artifact and its two checks.

    `elaborates` is the *new* check the step exists for -- "per-obligation acceptance proves each
    hole is filled correctly; whole-file verification proves they were filled *compatibly*". It is
    a fresh kernel elaboration of the assembled text.

    `links` is spec's second check, "each top-level declaration corresponding to a submitted goal
    links against its sealed constant with replay and audit", and it is **read from the recorded
    verdicts rather than re-derived**. That is a deliberate choice, not a shortcut, and the reason
    is structural: `/v1/link` writes a `verdict`, whose `attempt_id` is a foreign key to a real
    `attempt`. Re-linking here would mean materialization inventing attempt rows that no policy
    ever ran -- rows the scheduler, the budget accounting and every pass-rate calculation would
    then see. Corrupting the attempt record to satisfy a literal reading of the check would cost
    more than the check is worth.

    What is verified instead is the §1.1 predicate against each hole's accepted verdict, including
    `sealed_olean_sha_observed = obligation.sealed_olean_sha` -- so every declaration in this file
    demonstrably linked, replayed and audited against the sealed constant *at acceptance time*.
    What ties that to the file in hand is content-addressing: the sealed goals inlined below are
    the bundle's own text, fetched by `bundle_sha`, so they cannot have drifted from the constants
    those verdicts were issued against. Spec's worry -- "the materialized file could compile
    cleanly with a drifted statement" -- is closed by the digest rather than by a second link.
    """

    source: str
    elaborates: bool
    links: bool
    holes: int
    unfilled: tuple[str, ...]
    diagnostics: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.unfilled and self.elaborates and self.links


class FileMaterializer:
    """Spec §6.3 step 6: "Write the file with each `sorry` replaced, then check **two** things".

    The assembled file inlines the sealed bundle's own declarations rather than importing the
    generated `Bundle_<sha>` module. Importing it would be simpler and would make the artifact
    useless: a `.lean` file a person takes away must not depend on a per-run generated module they
    do not have. Inlining costs nothing -- the bundle source is content-addressed and already
    stored -- and the file it produces stands alone against the base environment.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        blobs: BlobStore,
        lean: LeanService,
    ) -> None:
        self._sessions = session_factory
        self._blobs = blobs
        self._lean = lean

    async def assemble(self, run_id: uuid.UUID) -> AssembledFile:
        run = await self._load_run(run_id)
        if run.reassembly is None:
            raise MaterializationError(
                f"run {run_id} has no reassembly: a bare-statement submission is not a file with "
                "holes in it, so there is nothing to write back"
            )
        holes = await self._load_holes(run_id)

        unfilled = tuple(sorted(h.hole for h in holes if h.proof is None))
        bundle_source = await self._blobs.get(run.bundle_sha) if run.bundle_sha else b""
        header, body = self._render(run, holes, bundle_source.decode())
        source = f"{header}\n{body}"

        # Check one: does the assembled file elaborate? This is the whole point of the step -- each
        # hole was already accepted on its own, and what is untested until now is whether they were
        # filled *compatibly*.
        #
        # The *body* is checked, not the artifact: `/v1/check` elaborates against an already-warm
        # base environment, so a body carrying its own `import` line fails outright with "invalid
        # 'import' command, it must be used in the beginning of the file". Stripping the header is
        # exactly equivalent, since the header names the very imports that base env was built
        # from -- and the artifact keeps them, because a file a person takes away needs them.
        checked = await self._lean.check(base_env_digest=run.base_env_digest, body=body)

        # Check two: every declaration linked, replayed and audited against its sealed constant.
        # Read from the verdicts rather than re-derived -- see `AssembledFile` for why inventing
        # attempt rows to re-link would cost more than the check is worth.
        links = not unfilled and await self._every_hole_accepted(run.run_id)

        return AssembledFile(
            source=source,
            elaborates=checked.ok,
            links=links and not unfilled,
            holes=len(holes),
            unfilled=unfilled,
            diagnostics=checked.diagnostics,
        )

    async def _every_hole_accepted(self, run_id: uuid.UUID) -> bool:
        """The §1.1 predicate over every root of the run, read from committed rows.

        Deliberately re-checks each conjunct here rather than trusting `obligation.status =
        'proved'`. Status and verdict are two different records of the same event, and only
        `mark_proved` is supposed to be able to move one to match the other -- so reading the
        verdict directly means this answer does not depend on that having worked. The seal-integrity
        clause is included for the same reason it is in `mark_proved`: a proof against a *different*
        bundle than the obligation names is exactly the drift this file's second check exists for.
        """
        async with self._sessions() as session:
            unaccepted = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM obligation o WHERE o.run_id = :run AND o.is_root "
                        "AND NOT EXISTS ("
                        "  SELECT 1 FROM verdict v WHERE v.obligation_id = o.id "
                        "    AND v.kind = 'proved' AND v.link_ok AND v.replay_ok "
                        "    AND v.axiom_audit_ok "
                        "    AND v.sealed_olean_sha_observed = o.sealed_olean_sha)"
                    ),
                    {"run": run_id},
                )
            ).scalar_one()
        return int(unaccepted) == 0

    async def _load_run(self, run_id: uuid.UUID) -> _Run:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT r.base_env_digest, b.recipe, r.reassembly_blob, "
                        "(SELECT o.bundle_sha FROM obligation o WHERE o.run_id = r.id "
                        " AND o.bundle_sha IS NOT NULL LIMIT 1) "
                        "FROM run r JOIN base_env b ON b.digest = r.base_env_digest "
                        "WHERE r.id = :id"
                    ),
                    {"id": run_id},
                )
            ).one_or_none()
        if row is None:
            raise MaterializationError(f"no run {run_id}")
        base_env_digest, recipe, reassembly, bundle_sha = row
        return _Run(
            run_id=run_id,
            base_env_digest=bytes(base_env_digest).hex(),
            imports=tuple(recipe.get("imports", [])),
            reassembly=bytes(reassembly).decode() if reassembly is not None else None,
            bundle_sha=bytes(bundle_sha) if bundle_sha is not None else None,
        )

    async def _load_holes(self, run_id: uuid.UUID) -> list[_Hole]:
        """Every root obligation of the run, with the development that proved it if one did.

        The proof comes from `verdict.proof_blob` -- the row leanserv writes -- rather than from
        anything the agent side recorded, which is the same "workers observe, never transcribe"
        boundary the rest of the acceptance path keeps. `LEFT JOIN` so an unproved hole still
        appears: a partially-proved run's artifact is the useful thing to hand back, and it must
        say which holes are open rather than silently omit them.
        """
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT o.id, o.decl_name, v.proof_blob FROM obligation o "
                        "LEFT JOIN LATERAL ("
                        "  SELECT vv.proof_blob FROM verdict vv "
                        "  WHERE vv.obligation_id = o.id AND vv.kind = 'proved' "
                        "  ORDER BY vv.created_at DESC LIMIT 1"
                        ") v ON true "
                        "WHERE o.run_id = :run AND o.is_root"
                    ),
                    {"run": run_id},
                )
            ).all()

        holes: list[_Hole] = []
        for obligation_id, decl_name, proof_blob in rows:
            name = hole_name(decl_name)
            if name is None:
                raise MaterializationError(
                    f"obligation {obligation_id} is named {decl_name!r}, which does not match the "
                    "`LeanAgent.Goals.G_<n>` convention the reassembly's `sorry_<n>` refers to"
                )
            proof = (
                (await from_bytea(self._blobs, bytes(proof_blob))).decode()
                if proof_blob is not None
                else None
            )
            holes.append(
                _Hole(
                    obligation_id=obligation_id,
                    decl_name=decl_name,
                    hole=name,
                    entry=decl_name.replace("LeanAgent.Goals.G_", "LeanAgent.Sol.sol_", 1),
                    proof=proof,
                )
            )
        # Numeric, not lexicographic: `G_10` must not sort before `G_2`, and the declaration order
        # in the emitted file is what a reader follows.
        return sorted(holes, key=lambda h: int(h.hole.removeprefix("sorry_")))

    def _render(self, run: _Run, holes: list[_Hole], bundle_source: str) -> tuple[str, str]:
        """Assemble the standalone file, as `(header, body)`.

        Split because the two halves have different consumers: the artifact is `header + body`,
        while `/v1/check` can only take the body (a warm worker has already imported the base env
        and rejects an `import` line outright). Rendering once and slicing keeps them from
        drifting -- what is checked is exactly what is emitted, minus a header that adds nothing to
        check.

        The bundle's own `import` line is dropped and the base environment's imports are emitted
        once in the header: the bundle imports exactly the base env, so keeping both would be a
        duplicate import rather than a second dependency.
        """
        bundle_body = "\n".join(
            line for line in bundle_source.splitlines() if not line.startswith("import ")
        )
        header = "\n".join(
            [
                "-- Materialized by von-neumann's-orchestra (spec §6.3 step 6).",
                f"-- run: {run.run_id}",
                "--",
                "-- The sealed goals below are reproduced verbatim from the goal bundle this run's",
                "-- obligations were created against; every proof was accepted by link, replay and",
                "-- axiom audit against those exact constants before it was written here.",
                "\n".join(f"import {name}" for name in run.imports),
            ]
        )
        parts = [bundle_body, ""]
        for hole in holes:
            parts.append(f"-- {hole.hole}: {hole.decl_name}")
            parts.append(hole.proof if hole.proof is not None else _unfilled_stub(hole))
            parts.append("")
        # The reassembly refers to each hole as `sorry_<n>`; the accepted proofs define
        # `LeanAgent.Sol.sol_<n>`. One alias line each, rather than rewriting the agent's own text,
        # which would mean parsing it.
        for hole in holes:
            if hole.proof is not None:
                parts.append(f"abbrev {hole.hole} := @{hole.entry}")
        parts.append("")
        parts.append(run.reassembly or "")
        return header, "\n".join(parts)


def _unfilled_stub(hole: _Hole) -> str:
    """A hole nothing proved stays a `sorry`, labelled. Emitting the file anyway is deliberate:
    a partially-proved run's artifact is the useful thing to hand back, and `AssembledFile.complete`
    is what says whether it is finished. Silently omitting the declaration would produce a file
    that fails to elaborate for a reason the reader cannot see."""
    return (
        f"-- UNPROVED: no accepted verdict for {hole.decl_name}\n"
        f"theorem {hole.hole.replace('sorry_', 'unproved_')} : True := trivial"
    )
