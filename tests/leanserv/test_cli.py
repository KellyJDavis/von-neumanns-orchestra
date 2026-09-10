"""M2.9 exit criterion: the CLI against the real public API.

Drives `lean_agent_cli.main.main(argv, client=...)` -- the real argument parsing, the real command
functions, the real httpx client -- against a `TestClient` wired to the genuine app, which is in
turn wired to a genuine leanserv and a genuine kernel. Nothing between the command line and the
Lean process is a stub.

The point of the milestone is that the CLI is *only* an httpx client of spec §6.1: if it can do
this, so can anyone else's client, which is the real test of whether §6.1 is a complete API rather
than a convenient subset of one.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lean_agent_api.app import create_app
from lean_agent_api.materialize import BundleMaterializer
from lean_agent_cli.client import ApiClient
from lean_agent_cli.main import EXIT_ERROR, EXIT_INCOMPLETE, EXIT_OK, main
from lean_agent_core.blobs import LocalBlobStore
from lean_agent_serv.api import create_app as create_leanserv_app
from lean_agent_serv.cache import VerificationCacheStore
from lean_agent_serv.pool import LeanReplPool, PoolConfig
from lean_agent_serv.verdicts import VerdictWriter
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_end_to_end import LeanServiceOverTestClient


@pytest.fixture
def bundle_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("cli_bundles")


@pytest.fixture
def base_env(admin_engine: Engine) -> Iterator[str]:
    digest = f"cli-{uuid.uuid4()}".encode()
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO base_env (digest, recipe, toolchain_rev, mathlib_rev) VALUES "
                "(:d, '{\"imports\": [\"Init\"]}', 'v4.33.1', 'deadbeef')"
            ),
            {"d": digest},
        )
        conn.commit()
    yield digest.hex()
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM base_env WHERE digest = :d"), {"d": digest})
        conn.commit()


@pytest.fixture
def cli(
    lake_project_dir: Path,
    leanserv_async_database_url: str,
    app_async_database_url: str,
    bundle_root: Path,
    tmp_path: Path,
) -> Iterator[ApiClient]:
    """An `ApiClient` over the real public API over the real leanserv.

    `TestClient` is an `httpx.Client` subclass, so it can be handed to `ApiClient` directly -- the
    client under test is the shipped one, talking to the shipped app, with only the socket removed.
    """
    lean_engine = create_async_engine(leanserv_async_database_url)
    lean_sessions = async_sessionmaker(lean_engine, expire_on_commit=False)
    lean_blobs = LocalBlobStore(tmp_path / "leanserv_blobs")
    pool = LeanReplPool(lake_project_dir, PoolConfig(max_total_workers=4, bundle_root=bundle_root))
    leanserv_app = create_leanserv_app(
        pool,
        VerificationCacheStore(lean_sessions, lean_blobs),
        VerdictWriter(lean_sessions, lean_blobs),
        lean_sessions,
    )

    api_engine = create_async_engine(app_async_database_url)
    api_sessions = async_sessionmaker(api_engine, expire_on_commit=False)
    api_blobs = LocalBlobStore(tmp_path / "api_blobs")

    with TestClient(leanserv_app) as leanserv_client:
        api_app = create_app(
            session_factory=api_sessions,
            lean=LeanServiceOverTestClient(leanserv_client),
            blobs=api_blobs,
            materializer=BundleMaterializer(
                session_factory=api_sessions,
                blobs=api_blobs,
                bundle_root=bundle_root,
                lake_project_dir=lake_project_dir,
            ),
        )
        with TestClient(api_app) as api_client:
            yield ApiClient(client=api_client)
    asyncio.run(api_engine.dispose())
    asyncio.run(lean_engine.dispose())


def _cleanup(admin_engine: Engine, run_id: str) -> None:
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": uuid.UUID(run_id)})
        conn.commit()


def _run_ids(admin_engine: Engine) -> list[str]:
    with admin_engine.connect() as conn:
        return [str(r[0]) for r in conn.execute(text("SELECT id FROM run")).all()]


def test_run_submits_a_statement_and_reports_the_obligation(
    admin_engine: Engine, cli: ApiClient, base_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["run", "--statement", "(2 : Nat) + 2 = 4", "--base-env", base_env, "--json"],
        client=cli,
    )
    created = json.loads(capsys.readouterr().out)
    try:
        assert code == EXIT_OK
        assert len(created["root_obligations"]) == 1
        assert created["seal_failures"] == []
    finally:
        _cleanup(admin_engine, created["run_id"])


def test_run_exits_one_when_nothing_sealed(
    admin_engine: Engine, cli: ApiClient, base_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 is "the answer is no", not "the system is broken" -- a submission whose goals all
    failed to seal is a well-formed request with a negative result, and a script has to be able to
    tell that from an unreachable server (exit 2)."""
    code = main(
        ["run", "--statement", "NoSuchIdentifier", "--base-env", base_env, "--json"], client=cli
    )
    created = json.loads(capsys.readouterr().out)
    try:
        assert code == EXIT_INCOMPLETE
        assert created["root_obligations"] == []
    finally:
        _cleanup(admin_engine, created["run_id"])


def test_run_reports_admission_signals_in_human_output(
    admin_engine: Engine, cli: ApiClient, base_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec §4.5's signals are non-blocking, so the only way they matter is if someone sees them.
    A CLI that accepted the submission silently would make them unreachable in practice."""
    code = main(["run", "--statement", "(2 : Nat) + 2 = 4", "--base-env", base_env], client=cli)
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "possible mis-formalization" in out
    for run_id in _run_ids(admin_engine):
        _cleanup(admin_engine, run_id)


def test_run_on_an_unknown_base_env_exits_two(
    cli: ApiClient, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["run", "--statement", "True", "--base-env", "ab" * 32], client=cli)
    assert code == EXIT_ERROR
    # The server's own `detail` reaches the user, not just a status code.
    assert "no base_env" in capsys.readouterr().err


def test_batch_submits_each_file_and_keeps_going_past_a_bad_one(
    admin_engine: Engine,
    cli: ApiClient,
    base_env: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A batch that stopped at its worst input would be hostage to it -- for a benchmark folder,
    files that do not elaborate are the normal case rather than the exception."""
    good = tmp_path / "good.lean"
    good.write_text("theorem g : (1 : Nat) + 1 = 2 := by sorry")
    bad = tmp_path / "bad.lean"
    bad.write_text('theorem b : (1 : Nat) + 1 = "no" := by sorry')

    code = main(["batch", str(good), str(bad), "--base-env", base_env, "--json"], client=cli)
    results = json.loads(capsys.readouterr().out)
    try:
        # Both were submitted; the second produced no obligations rather than aborting the batch.
        assert len(results) == 2
        assert len(results[0]["created"]["root_obligations"]) == 1
        assert results[1]["created"]["root_obligations"] == []
        assert code == EXIT_INCOMPLETE
    finally:
        for result in results:
            _cleanup(admin_engine, result["created"]["run_id"])


def test_from_folder_globs_recursively(
    admin_engine: Engine,
    cli: ApiClient,
    base_env: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "corpus"
    (folder / "nested").mkdir(parents=True)
    (folder / "a.lean").write_text("theorem a : (1 : Nat) + 1 = 2 := by sorry")
    (folder / "nested" / "b.lean").write_text("theorem b : (2 : Nat) + 2 = 4 := by sorry")
    (folder / "ignored.txt").write_text("not lean")

    code = main(["from-folder", str(folder), "--base-env", base_env, "--json"], client=cli)
    results = json.loads(capsys.readouterr().out)
    try:
        assert code == EXIT_OK
        assert [Path(r["file"]).name for r in results] == ["a.lean", "b.lean"]
    finally:
        for result in results:
            _cleanup(admin_engine, result["created"]["run_id"])


def test_from_folder_with_no_lean_files_exits_two(
    cli: ApiClient, base_env: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["from-folder", str(empty), "--base-env", base_env], client=cli) == EXIT_ERROR
    assert "no .lean files" in capsys.readouterr().err


def test_status_shows_counts_spend_and_obligations(
    admin_engine: Engine, cli: ApiClient, base_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["run", "--statement", "(2 : Nat) + 2 = 4", "--base-env", base_env, "--json"], client=cli)
    created = json.loads(capsys.readouterr().out)
    try:
        code = main(["status", created["run_id"], "--obligations"], client=cli)
        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert "open=1" in out
        assert "spend: 0 tokens" in out
        assert created["root_obligations"][0] in out
    finally:
        _cleanup(admin_engine, created["run_id"])


def test_materialize_writes_the_artifact_and_flags_an_unfilled_hole(
    admin_engine: Engine,
    cli: ApiClient,
    base_env: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The file is written even when incomplete, and the exit code says so.

    Refusing to write would make the failure mode "no output at all" rather than "output with the
    gaps marked" -- and the artifact already names its open holes.
    """
    source = tmp_path / "holed.lean"
    source.write_text("theorem t : (1 : Nat) + 1 = 2 := by sorry")
    main(["run", "--file", str(source), "--base-env", base_env, "--json"], client=cli)
    created = json.loads(capsys.readouterr().out)
    out_path = tmp_path / "materialized.lean"
    try:
        code = main(["materialize", created["run_id"], "-o", str(out_path)], client=cli)
        assert code == EXIT_INCOMPLETE
        assert "unfilled hole(s)" in capsys.readouterr().err
        # Written anyway, and it says which hole is open.
        assert "UNPROVED" in out_path.read_text()
        assert "theorem t :" in out_path.read_text()
    finally:
        _cleanup(admin_engine, created["run_id"])


def test_materialize_for_a_bare_statement_exits_two(
    admin_engine: Engine, cli: ApiClient, base_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A statement submission has no file with holes, so the API answers 409 -- a request error,
    not a negative result about a proof."""
    main(["run", "--statement", "True", "--base-env", base_env, "--json"], client=cli)
    created = json.loads(capsys.readouterr().out)
    try:
        assert main(["materialize", created["run_id"]], client=cli) == EXIT_ERROR
        assert "no reassembly" in capsys.readouterr().err
    finally:
        _cleanup(admin_engine, created["run_id"])


def test_json_output_is_the_api_s_own_object(
    admin_engine: Engine, cli: ApiClient, base_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reshaping would make the CLI a second, undocumented schema drifting from the OpenAPI
    document. `--json` prints what the API said."""
    main(["run", "--statement", "True", "--base-env", base_env, "--json"], client=cli)
    created = json.loads(capsys.readouterr().out)
    try:
        assert set(created) == {
            "run_id",
            "manifest_hash",
            "root_obligations",
            "admission",
            "seal_failures",
        }
    finally:
        _cleanup(admin_engine, created["run_id"])


def test_a_run_and_a_statement_are_mutually_exclusive(cli: ApiClient) -> None:
    """argparse enforces it, so it is a usage error rather than a request the server has to
    reject."""
    with pytest.raises(SystemExit) as exc_info:
        main(
            ["run", "--file", "x.lean", "--statement", "True", "--base-env", "ab" * 32], client=cli
        )
    assert exc_info.value.code == 2
