"""Phase 1 exit gate 9: prelude memory delta (R19 input).

Measures the RSS memory delta between a warm worker holding only a curated base-env import set
and one holding that same base plus one additional "prelude" import -- spec's own term (§6.2) for
a project-local library layered on top of a base environment: "Project-local libraries are
handled as *preludes* within a base environment, which removes re-elaboration cost -- but *not*
memory cost, since a prelude-bearing worker occupies a full slot." Gate 9 asks for that memory
cost, measured, feeding directly into R19 ("Zygote fork may not be viable... The multi-tenancy
capacity argument rests on it... If it fails, deployment shifts from shared nodes to per-tenant
nodes"): a small per-declaration prelude cost weakens the urgency for a copy-on-write zygote fork;
a large one strengthens it.

Deliberately a script producing a report, not a test with a pass/fail assertion -- there is no
"correct" delta to check against, only a number (or a small cost curve) to measure and hand to
R19's own judgment call. Run manually/periodically, the same way gate 7's `decompose-fuzz` isn't
part of the fast per-commit test loop either.

Each repeat pairs a *fresh* base measurement with the prelude measurement, rather than reusing one
shared base across every round -- confirmed necessary empirically, not a defensive-only choice: a
single base measurement of a ~1.5 GiB warm Mathlib worker varied by ~2 MiB run to run on its own
(system-level RSS jitter -- allocator/GC timing, not this module's own logic), which is *larger*
than the true delta of a modestly-sized prelude. Pairing each round's own base against that same
round's own prelude measurement controls for that drift far better than one base reused across an
entire session; see CLAUDE.md for the full empirical writeup, including why small prelude sizes
(tens to low thousands of declarations) are simply invisible against this noise floor and a much
larger sample is needed for a clean reading at all.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lean_agent_serv.repl import ReplWorker


def _process_group_rss_kib(root_pid: int) -> int:
    """Total RSS, in KiB, of `root_pid` and its direct children -- not just `root_pid` alone.
    `ReplWorker.pid` is the `lake` process's own pid, but `lake exe`/`lake env` forks the actual
    `leankernel` binary as a *child* process rather than exec-replacing itself (the same fact
    M1.8.2 found the hard way for killing a worker); the real memory cost of a warm environment
    lives in that child, not in `lake` itself, so measuring only `root_pid` would report roughly
    the supervisor's own (small) footprint instead of the worker's real one.

    Implemented via a plain `ps -A` listing filtered in Python for `pid == root_pid` or
    `ppid == root_pid`, rather than `ps`'s own process-group selection flags -- those differ in
    meaning between BSD-style `ps` (macOS) and Linux's procps, where `-A`/`-e` and a `pid,ppid,rss`
    column format are both consistently supported.
    """
    output = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True, check=True
    ).stdout
    total = 0
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        pid, ppid, rss = int(fields[0]), int(fields[1]), int(fields[2])
        if pid == root_pid or ppid == root_pid:
            total += rss
    return total


async def measure_worker_rss_kib(lake_project_dir: Path, imports: tuple[str, ...]) -> int:
    """Spawn a worker with `imports`, confirm it's genuinely warm (not merely started -- `spawn`
    returning only means the process launched, not that `Serve.lean`'s own import-then-loop
    startup has finished), measure its process group's RSS, then close it.
    """
    worker = await ReplWorker.spawn(lake_project_dir, imports)
    try:
        await worker.check("theorem gate9_probe : True := trivial")
        return _process_group_rss_kib(worker.pid)
    finally:
        await worker.close()


@dataclass(frozen=True)
class PreludeDeltaResult:
    prelude_import: str
    base_rss_kib: int
    with_prelude_rss_kib: int

    @property
    def delta_kib(self) -> int:
        return self.with_prelude_rss_kib - self.base_rss_kib


async def measure_prelude_deltas(
    lake_project_dir: Path,
    base_imports: tuple[str, ...],
    prelude_imports: tuple[str, ...],
    *,
    repeats: int = 1,
) -> dict[str, list[PreludeDeltaResult]]:
    """One `(base, base+prelude)` pair per repeat per prelude, each pair measured back-to-back --
    see the module docstring for why a fresh base every round, not one base shared across the
    whole session, is what makes the resulting deltas trustworthy rather than noise-dominated.
    """
    results: dict[str, list[PreludeDeltaResult]] = {p: [] for p in prelude_imports}
    for _ in range(repeats):
        for prelude_import in prelude_imports:
            base_rss_kib = await measure_worker_rss_kib(lake_project_dir, base_imports)
            with_prelude_rss_kib = await measure_worker_rss_kib(
                lake_project_dir, (*base_imports, prelude_import)
            )
            results[prelude_import].append(
                PreludeDeltaResult(prelude_import, base_rss_kib, with_prelude_rss_kib)
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake-project-dir", type=Path, default=Path("packages/leankernel"))
    parser.add_argument(
        "--base-import",
        action="append",
        default=None,
        help="Curated base-env import (repeatable). Defaults to Mathlib.Algebra.Group.Basic.",
    )
    parser.add_argument(
        "--prelude-import",
        action="append",
        required=True,
        help="A module to measure as an additional prelude on top of the base (repeatable).",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Paired (base, base+prelude) measurements per prelude, to average out RSS jitter.",
    )
    args = parser.parse_args()
    base_imports = tuple(args.base_import or ["Mathlib.Algebra.Group.Basic"])

    results = asyncio.run(
        measure_prelude_deltas(
            args.lake_project_dir,
            base_imports,
            tuple(args.prelude_import),
            repeats=args.repeats,
        )
    )
    for prelude_import, runs in results.items():
        deltas = [r.delta_kib for r in runs]
        mean = statistics.mean(deltas)
        spread = f"min {min(deltas):+} / max {max(deltas):+}" if len(deltas) > 1 else ""
        print(
            f"{prelude_import}: mean delta {mean:+.0f} KiB ({mean / 1024:+.1f} MiB) "
            f"over {len(deltas)} run(s) {spread}"
        )


if __name__ == "__main__":
    main()
