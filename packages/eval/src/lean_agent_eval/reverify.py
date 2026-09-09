"""Clean re-verification (spec §7.5: "re-verify every claimed success from scratch in a clean
container from the manifest via `lake check --paranoid`").

Two deviations from that literal wording, both confirmed empirically rather than assumed:

1. **No `lake check` subcommand, and no `--paranoid` flag, exist in Lake 5.0.0-src (the version
   pinned by v4.33.1's toolchain)** -- `lake --help` lists `build`/`test`/`env`/`lean`/... but no
   `check`. What actually re-verifies a file from scratch is `lake env lean <file>`: elaboration
   *is* kernel type-checking in Lean (there's no separate lighter "check-only" mode, per M1.1's own
   established finding), so a plain `lake env lean <file>` genuinely re-verifies the file, and its
   exit code is reliable (confirmed against both a real passing and a real failing file: 0 and 1
   respectively, with diagnostics on stderr for the failure). "paranoid" in *this* codebase's own
   vocabulary already means something concrete elsewhere -- `LinkRequest.paranoid` (multi-kernel
   replay, spec's own field) and Appendix B's `paranoid_replay` config knob -- neither of which
   `/v1/link` implements yet (M1.8.5 deferred it), so there is no multi-kernel replay to add here
   either without duplicating that unbuilt work.
2. **"Clean container" here means a brand-new OS process, not literal Docker isolation.** No
   container-orchestration code exists anywhere in this codebase yet (`deploy/Dockerfile.base` is
   an image definition, not something this package launches) -- building that just for this eval
   skeleton, with no other caller that would need it, would be exactly the speculative
   infrastructure this project avoids. A fresh `lake env lean` subprocess still satisfies the
   actual intent (independent of whatever process produced the claimed success, no warm REPL, no
   shared `Environment` state) without inventing unused container-launching machinery.
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import os
import signal
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class ReverifyResult:
    ok: bool
    returncode: int
    diagnostics: str


async def reverify_file(
    lake_project_dir: Path, lean_file: Path, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> ReverifyResult:
    """Elaborate `lean_file` in a fresh `lean` process run via `lake env` (so it resolves the same
    `LEAN_PATH`/dependencies as `lake_project_dir`'s own build), wholly independent of whatever
    produced the claimed success. `start_new_session=True` + `killpg` on timeout, not a plain
    `kill()` -- the same process-group lesson M1.8.2 learned the hard way for `lake exe`: a `lake`
    subcommand is not guaranteed to exec-replace itself rather than forking a child, so signalling
    only the direct child can leave the real work orphaned and running.
    """
    process = await asyncio.create_subprocess_exec(
        "lake",
        "env",
        "lean",
        str(lean_file),
        cwd=lake_project_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(timeout_s):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        return ReverifyResult(ok=False, returncode=-1, diagnostics=f"timed out after {timeout_s}s")

    diagnostics = (stdout + stderr).decode(errors="replace")
    returncode = process.returncode if process.returncode is not None else -1
    return ReverifyResult(ok=returncode == 0, returncode=returncode, diagnostics=diagnostics)
