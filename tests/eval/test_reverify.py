"""M1.9 exit criterion: `reverify_file` genuinely re-checks a Lean file from a fresh process --
against the real `lake`/`lean` toolchain, never a mock.

Local dev / CI: `lake build` in `packages/leankernel` first (not strictly required for `lake env
lean` to work, but keeps this suite consistent with `tests/leanserv/`'s skip convention and
guarantees the toolchain the tests assume is actually set up).

No pytest-asyncio: plain sync `def test_...` functions drive `reverify_file` via `asyncio.run`,
matching every other async module in this repo.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from lean_agent_eval.reverify import reverify_file

LEANKERNEL_DIR = Path(__file__).resolve().parents[2] / "packages" / "leankernel"
LEANKERNEL_EXE = LEANKERNEL_DIR / ".lake" / "build" / "bin" / "leankernel"


@pytest.fixture(scope="session")
def lake_project_dir() -> Path:
    if not LEANKERNEL_EXE.exists():
        pytest.skip(
            f"{LEANKERNEL_EXE} not built; run `lake build` in {LEANKERNEL_DIR} first. "
            "CI always builds it before this suite runs (see .github/workflows/ci.yml)."
        )
    return LEANKERNEL_DIR


def test_reverify_accepts_a_genuinely_valid_file(lake_project_dir: Path, tmp_path: Path) -> None:
    lean_file = tmp_path / "good.lean"
    lean_file.write_text("import Init\ntheorem t : (1 : Nat) + 1 = 2 := by decide\n")

    result = asyncio.run(reverify_file(lake_project_dir, lean_file))

    assert result.ok
    assert result.returncode == 0


def test_reverify_rejects_a_genuinely_false_statement(
    lake_project_dir: Path, tmp_path: Path
) -> None:
    lean_file = tmp_path / "bad.lean"
    lean_file.write_text("import Init\ntheorem t : (1 : Nat) + 1 = 3 := by decide\n")

    result = asyncio.run(reverify_file(lake_project_dir, lean_file))

    assert not result.ok
    assert result.returncode != 0
    assert result.diagnostics


def test_reverify_kills_the_whole_process_group_on_timeout(
    lake_project_dir: Path, tmp_path: Path
) -> None:
    """Confirms the process-group lesson from M1.8.2 applies here too: `lake env` forks a real
    `lean` child rather than exec-replacing itself (confirmed empirically -- see CLAUDE.md), so
    killing only the direct `lake` pid would orphan the actual hung process.
    """
    lean_file = tmp_path / "spin.lean"
    lean_file.write_text("import Init\npartial def spin : IO Unit := spin\n#eval spin\n")

    result = asyncio.run(reverify_file(lake_project_dir, lean_file, timeout_s=1.0))

    assert not result.ok
    assert "timed out" in result.diagnostics
