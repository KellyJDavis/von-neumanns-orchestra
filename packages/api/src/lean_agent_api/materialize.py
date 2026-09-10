"""Bundle materialization: compile a sealed bundle out of band and record its digest (spec §4.1).

Spec §4.1 leaves the `.olean` deliberately unbuilt on the hot path -- "produced lazily out of band
for reproducibility and `lean4checker` batching; the hot path never waits on the build system".
This module is that out-of-band step, and until it runs an obligation cannot be proved at all:
Link requires the sealed goal to be a genuinely *imported* constant, and `mark_proved` compares
the observed digest against `obligation.sealed_olean_sha`, which ingestion leaves NULL.

So materialization is not an optimization. It is the step that turns an obligation from "recorded"
into "provable", and nothing else in the pipeline can substitute for it.

**What is not here.** Spec §6.3 step 6's *output file* materialization -- "write the file with each
`sorry` replaced, then check the assembled file elaborates *and* each top-level declaration links
against its sealed constant" -- is a different artifact and needs one thing nothing records yet:
the reassembly text `/v1/decompose` produced, which has no column and whose natural home
(`obligation_edge.reassembly_blob`, "on the group, not the child") does not apply, because
ingestion creates roots and no edges. Deciding where it lives is tangled with the decomposition-edge
design that M2.4's own seam already defers. The half that *is* recorded is in place:
`/v1/link` now stores the accepted development as `verdict.proof_blob`, which is where the assembly
step will read each hole's filling from.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from lean_agent_core.protocols import BlobStore
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
