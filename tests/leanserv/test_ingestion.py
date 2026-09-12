"""M2.6 exit criterion: ingestion (spec §6.3 steps 1-4) end to end.

A real `.lean` file with `sorry`s goes in; a real run and real root obligations come out, with
every goal genuinely elaborated and sealed by a real kernel through real `/v1/decompose` and
`/v1/seal`, against a real PostgreSQL.

The `LeanService` here is the same `TestClient`-backed one M2.5's null-agent test uses, extended
with `seal`/`decompose` -- see that file for why the deployed httpx client belongs with M2.9.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from conftest import MaterializedBundle
from fastapi.testclient import TestClient
from lean_agent_api.ingestion import Ingestor, Submission, build_manifest, canonical_manifest_hash
from lean_agent_core.blobs import LocalBlobStore
from lean_agent_core.protocols import (
    CheckOutcome,
    DecomposedLemma,
    DecomposeOutcome,
    LinkOutcome,
    SealedGoal,
    SealGoalRequest,
    SealOutcome,
)
from lean_agent_serv.api import create_app
from lean_agent_serv.cache import VerificationCacheStore
from lean_agent_serv.pool import LeanReplPool, PoolConfig
from lean_agent_serv.verdicts import VerdictWriter
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class LeanServiceOverTestClient:
    """A `LeanService` over the real leanserv app."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    async def check(
        self,
        *,
        base_env_digest: str,
        body: str,
        bundle_sha: str | None = None,
        timeout_ms: int | None = None,
    ) -> CheckOutcome:
        payload: dict[str, object] = {"base_env_digest": base_env_digest, "body": body}
        if bundle_sha is not None:
            payload["bundle_sha"] = bundle_sha
        if timeout_ms is not None:
            payload["timeout_ms"] = timeout_ms
        data = self._post("/v1/check", payload)
        return CheckOutcome(
            ok=data["ok"],
            diagnostics=tuple(data["diagnostics"]),
            cache_hit=data["cache_hit"],
            elapsed_ms=data["elapsed_ms"],
        )

    async def seal(
        self,
        *,
        base_env_digest: str,
        goals: Sequence[SealGoalRequest],
        timeout_ms: int | None = None,
    ) -> SealOutcome:
        del timeout_ms
        data = self._post(
            "/v1/seal",
            {
                "base_env_digest": base_env_digest,
                "goals": [
                    {
                        "name": g.name,
                        "statement": g.statement,
                        "level_params": list(g.level_params),
                    }
                    for g in goals
                ],
            },
        )
        return SealOutcome(
            ok=data["ok"],
            goals=tuple(
                SealedGoal(
                    decl_name=g["decl_name"],
                    goal_src=g["goal_src"],
                    goal_digest=g["goal_digest"],
                    level_params=tuple(g["level_params"]),
                    diagnostics=tuple(g["diagnostics"]),
                    ok=g["ok"],
                )
                for g in data["goals"]
            ),
            bundle_source=data["bundle_source"],
            bundle_digest=data["bundle_digest"],
        )

    async def decompose(
        self, *, base_env_digest: str, development: str, timeout_ms: int | None = None
    ) -> DecomposeOutcome:
        del timeout_ms
        data = self._post(
            "/v1/decompose", {"base_env_digest": base_env_digest, "development": development}
        )
        return DecomposeOutcome(
            ok=data["ok"],
            lemmas=tuple(
                DecomposedLemma(
                    name=lemma["name"],
                    statement=lemma["statement"],
                    level_params=tuple(lemma["level_params"]),
                    round_trips=lemma["round_trips"],
                    diagnostics=tuple(lemma["diagnostics"]),
                )
                for lemma in data["lemmas"]
            ),
            reassembly=data["reassembly"],
            diagnostics=tuple(data["diagnostics"]),
        )

    async def link(
        self,
        *,
        attempt_id: uuid.UUID,
        obligation_id: uuid.UUID,
        base_env_digest: str,
        bundle_sha: str,
        goal: str,
        entry: str,
        development: str,
        timeout_ms: int | None = None,
    ) -> LinkOutcome:  # pragma: no cover - ingestion never links
        raise NotImplementedError

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        response = self._client.post(path, json=payload)
        response.raise_for_status()
        return dict(response.json())


@pytest.fixture
def base_env(admin_engine: Engine) -> Iterator[str]:
    digest = f"ingest-{uuid.uuid4()}".encode()
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
def leanserv(
    lake_project_dir: Path,
    leanserv_async_database_url: str,
    tmp_path: Path,
    materialized_bundle: MaterializedBundle,
) -> Iterator[TestClient]:
    engine = create_async_engine(leanserv_async_database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    blobs = LocalBlobStore(tmp_path)
    pool = LeanReplPool(
        lake_project_dir, PoolConfig(max_total_workers=4, bundle_root=materialized_bundle.root)
    )
    app = create_app(
        pool,
        VerificationCacheStore(sessionmaker, blobs),
        VerdictWriter(sessionmaker, blobs),
        sessionmaker,
    )
    with TestClient(app) as client:
        yield client
    asyncio.run(engine.dispose())


def _ingest(
    app_async_database_url: str, tmp_path: Path, leanserv: TestClient, submission: Submission
) -> object:
    async def main() -> object:
        engine = create_async_engine(app_async_database_url)
        try:
            ingestor = Ingestor(
                session_factory=async_sessionmaker(engine, expire_on_commit=False),
                lean=LeanServiceOverTestClient(leanserv),
                blobs=LocalBlobStore(tmp_path),
            )
            return await ingestor.ingest(submission)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _cleanup(admin_engine: Engine, run_id: uuid.UUID) -> None:
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": run_id})
        conn.commit()


def test_a_file_with_sorries_becomes_one_root_obligation_per_site(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """Spec §6.3 steps 3-4, for real: elaborate once, extract the `sorry` sites, seal each.

    Two sites, so two roots -- and no edges, because a submitted file has no parent obligation to
    hang them from (see `ingestion.py` on why reassembling a *file* is materialization, not a DAG
    edge).
    """
    submission = Submission(
        base_env_digest=base_env,
        tenant_id=uuid.uuid4(),
        source=(
            "theorem p : (1 : Nat) + 1 = 2 ∧ (2 : Nat) + 2 = 4 := by\n"
            "  constructor\n  · sorry\n  · sorry"
        ),
    )
    result = _ingest(app_async_database_url, tmp_path, leanserv, submission)
    try:
        assert len(result.root_obligations) == 2  # type: ignore[attr-defined]
        assert result.seal_failures == []  # type: ignore[attr-defined]
        with admin_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT decl_name, goal_src, is_root, depth, bundle_sha, sealed_olean_sha "
                    "FROM obligation WHERE run_id = :r ORDER BY decl_name"
                ),
                {"r": result.run_id},  # type: ignore[attr-defined]
            ).all()
            edges = conn.execute(
                text(
                    "SELECT count(*) FROM obligation_edge e JOIN obligation o "
                    "ON o.id = e.parent_id WHERE o.run_id = :r"
                ),
                {"r": result.run_id},  # type: ignore[attr-defined]
            ).scalar_one()
        assert [r[0] for r in rows] == ["LeanAgent.Goals.G_1", "LeanAgent.Goals.G_2"]
        assert [r[1] for r in rows] == ["1 + 1 = 2", "2 + 2 = 4"]
        assert all(r[2] is True and r[3] == 0 for r in rows)
        # Every obligation names the bundle it links against...
        assert all(r[4] is not None for r in rows)
        # ...and none has a compiled `.olean` yet: §4.1 builds it lazily out of band, and
        # `mark_proved` compares against this column, so NULL means "cannot be proved until M2.7
        # materializes it" -- which is the correct state, not a gap.
        assert all(r[5] is None for r in rows)
        assert edges == 0
    finally:
        _cleanup(admin_engine, result.run_id)  # type: ignore[attr-defined]


def test_a_bare_statement_becomes_one_obligation(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    submission = Submission(
        base_env_digest=base_env,
        tenant_id=uuid.uuid4(),
        statement="∀ n : Nat, n + 0 = n",
    )
    result = _ingest(app_async_database_url, tmp_path, leanserv, submission)
    try:
        (obligation,) = result.root_obligations  # type: ignore[attr-defined]
        with admin_engine.connect() as conn:
            decl, src = conn.execute(
                text("SELECT decl_name, goal_src FROM obligation WHERE id = :id"),
                {"id": obligation},
            ).one()
        assert decl == "LeanAgent.Goals.G_1"
        assert src == "∀ n : Nat, n + 0 = n"
    finally:
        _cleanup(admin_engine, result.run_id)  # type: ignore[attr-defined]


def test_a_statement_that_does_not_elaborate_is_reported_not_raised(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """Spec §6.1: `seal_failures` "is returned rather than raised". The run is still created --
    a submission that produced no obligations is a run with nothing to do, not a failed request.
    """
    submission = Submission(
        base_env_digest=base_env, tenant_id=uuid.uuid4(), statement="NoSuchIdentifier"
    )
    result = _ingest(app_async_database_url, tmp_path, leanserv, submission)
    try:
        assert result.root_obligations == []  # type: ignore[attr-defined]
        (failure,) = result.seal_failures  # type: ignore[attr-defined]
        assert failure.reason == "did not elaborate"
        assert any("NoSuchIdentifier" in d for d in failure.diagnostics)
    finally:
        _cleanup(admin_engine, result.run_id)  # type: ignore[attr-defined]


def test_a_file_that_does_not_elaborate_reports_rather_than_raises(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """A file whose *own* elaboration fails yields no obligations and one reported failure.

    Worth pinning because it is a different case from spec §6.1's "ten goals of which one does not
    elaborate", and the difference is structural: `/v1/decompose` gates on the whole development
    elaborating, so a broken declaration anywhere means no `sorry` site is extracted at all --
    there is no partial success to report. §6.1's per-goal case lives at the `/v1/seal` boundary
    and is tested there (`test_api.py`), and ingestion carries it through in `_candidates`.

    The run is still created either way: a submission that produced no obligations is a run with
    nothing to do, not a failed request.
    """
    submission = Submission(
        base_env_digest=base_env,
        tenant_id=uuid.uuid4(),
        # A genuine type error, not an unknown identifier: an unknown identifier is *auto-bound*
        # with `autoImplicit` at its default, so the file elaborates and yields obligations -- see
        # `test_admission_flags_a_source_that_only_elaborates_with_auto_implicit`.
        source='theorem bad : (1 : Nat) + 1 = "not a number" := by sorry',
    )
    result = _ingest(app_async_database_url, tmp_path, leanserv, submission)
    try:
        assert result.root_obligations == []  # type: ignore[attr-defined]
        (failure,) = result.seal_failures  # type: ignore[attr-defined]
        assert failure.reason == "the submitted file does not elaborate"
        assert failure.diagnostics
        with admin_engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM run WHERE id = :id"),
                    {"id": result.run_id},  # type: ignore[attr-defined]
                ).scalar_one()
                == 1
            )
    finally:
        _cleanup(admin_engine, result.run_id)  # type: ignore[attr-defined]


def test_admission_records_a_trivially_closed_root(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """Spec §4.5's first signal. Recorded as *what happened* (`closed_by`, `closed_in_ms`) rather
    than as a judgement, because the threshold that turns "closed quickly" into "likely
    mis-formalization" carries a **[measure]** marker in spec and has not been measured.
    """
    submission = Submission(
        base_env_digest=base_env, tenant_id=uuid.uuid4(), statement="(2 : Nat) + 2 = 4"
    )
    result = _ingest(app_async_database_url, tmp_path, leanserv, submission)
    try:
        (obligation,) = result.root_obligations  # type: ignore[attr-defined]
        report = result.admission[obligation]  # type: ignore[attr-defined]
        assert report.closed_by in {"simp", "decide"}
        assert report.closed_in_ms is not None
        assert report.needs_auto_implicit is False
        with admin_engine.connect() as conn:
            stored = conn.execute(
                text("SELECT admission FROM obligation WHERE id = :id"), {"id": obligation}
            ).scalar_one()
        assert stored["closed_by"] == report.closed_by
    finally:
        _cleanup(admin_engine, result.run_id)  # type: ignore[attr-defined]


def test_admission_is_quiet_on_a_goal_nothing_closes(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """The signal must not fire on ordinary work, or it says nothing at all.

    `∀ n m : Nat, n + m = m + n` was the first choice and is a bad one: `exact?` finds
    `Nat.add_comm` in core and closes it in ~230 ms, which is the signal working correctly on a
    statement that really is trivially closable. Needs a goal that genuinely requires induction.
    """
    submission = Submission(
        base_env_digest=base_env,
        tenant_id=uuid.uuid4(),
        statement="∀ n : Nat, 2 ^ n ≥ n + 1",
    )
    result = _ingest(app_async_database_url, tmp_path, leanserv, submission)
    try:
        (obligation,) = result.root_obligations  # type: ignore[attr-defined]
        assert result.admission[obligation].closed_by is None  # type: ignore[attr-defined]
    finally:
        _cleanup(admin_engine, result.run_id)  # type: ignore[attr-defined]


def test_the_run_manifest_is_frozen_and_hashed(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """Spec §7.3: "frozen at creation and included in every published result". The hash must be a
    function of the manifest's content, not of the order this process built the dict in."""
    submission = Submission(base_env_digest=base_env, tenant_id=uuid.uuid4(), statement="True")
    result = _ingest(app_async_database_url, tmp_path, leanserv, submission)
    try:
        with admin_engine.connect() as conn:
            manifest, stored_hash = conn.execute(
                text("SELECT manifest, manifest_hash FROM run WHERE id = :id"),
                {"id": result.run_id},  # type: ignore[attr-defined]
            ).one()
        assert bytes(stored_hash).hex() == result.manifest_hash  # type: ignore[attr-defined]
        assert canonical_manifest_hash(manifest) == bytes(stored_hash)
        # Zero model calls is the claim a published Phase 2 result has to make, so an empty list
        # rather than an absent key -- those say different things.
        assert manifest["models"] == []
    finally:
        _cleanup(admin_engine, result.run_id)  # type: ignore[attr-defined]


def test_manifest_hash_ignores_key_order() -> None:
    submission = Submission(base_env_digest="ab" * 32, tenant_id=uuid.UUID(int=1), statement="True")
    manifest = build_manifest(submission, {"toolchain_rev": "v4.33.1", "mathlib_rev": "abc"})
    shuffled = dict(reversed(list(manifest.items())))
    assert canonical_manifest_hash(manifest) == canonical_manifest_hash(shuffled)


def test_a_submission_must_carry_exactly_one_of_source_or_statement(
    base_env: str, leanserv: TestClient, app_async_database_url: str, tmp_path: Path
) -> None:
    for kwargs in ({}, {"source": "theorem t : True := by sorry", "statement": "True"}):
        with pytest.raises(ValueError, match="exactly one"):
            _ingest(
                app_async_database_url,
                tmp_path,
                leanserv,
                Submission(base_env_digest=base_env, tenant_id=uuid.uuid4(), **kwargs),  # type: ignore[arg-type]
            )


def test_admission_flags_a_source_that_only_elaborates_with_auto_implicit(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """Spec §4.5's "elaborates only with `autoImplicit true`" signal, on the case that actually
    produces it.

    `NoSuchIdentifier` here is not an error: `/v1/decompose` elaborates at Lean's default, so it is
    silently auto-bound as a binder and the file yields perfectly well-formed obligations whose
    statements now quantify over a typo. That is precisely the mis-formalization §4.5 wants
    flagged, and it must be measured on the *source* -- by the time the statement is abstracted the
    generalization is written down as an honest explicit binder and there is nothing left to see.
    """
    submission = Submission(
        base_env_digest=base_env,
        tenant_id=uuid.uuid4(),
        source="theorem typo : (1 : Nat) + 1 = NoSuchIdentifier := by sorry",
    )
    result = _ingest(app_async_database_url, tmp_path, leanserv, submission)
    try:
        # It really did produce an obligation -- the submission was admitted, not rejected.
        (obligation,) = result.root_obligations  # type: ignore[attr-defined]
        assert result.admission[obligation].needs_auto_implicit is True  # type: ignore[attr-defined]
        with admin_engine.connect() as conn:
            stored = conn.execute(
                text("SELECT admission, goal_src FROM obligation WHERE id = :id"),
                {"id": obligation},
            ).one()
        assert stored[0]["needs_auto_implicit"] is True
        # The typo is now a binder in the sealed statement, which is exactly why the signal has to
        # be recorded rather than re-derived later.
        assert "NoSuchIdentifier" in stored[1]
    finally:
        _cleanup(admin_engine, result.run_id)  # type: ignore[attr-defined]


def test_the_submissions_attempt_budget_reaches_every_obligation(
    admin_engine: Engine,
    base_env: str,
    leanserv: TestClient,
    app_async_database_url: str,
    tmp_path: Path,
) -> None:
    """M3.12. The budget was written into the run manifest and nowhere else, so every obligation
    ran on the column's default of 8 whatever was asked. A one-attempt evaluation run found it: a
    failed problem was quietly attempted again, turning pass@n into pass@n x 8."""
    submission = Submission(
        base_env_digest=base_env,
        tenant_id=uuid.uuid4(),
        source=(
            "theorem p : (1 : Nat) + 1 = 2 ∧ (2 : Nat) + 2 = 4 := by\n"
            "  constructor\n  · sorry\n  · sorry"
        ),
        budget_attempts=1,
    )
    result = _ingest(app_async_database_url, tmp_path, leanserv, submission)
    try:
        with admin_engine.connect() as conn:
            budgets = (
                conn.execute(
                    text("SELECT budget_attempts FROM obligation WHERE run_id = :r"),
                    {"r": result.run_id},  # type: ignore[attr-defined]
                )
                .scalars()
                .all()
            )
        assert budgets == [1, 1]
    finally:
        _cleanup(admin_engine, result.run_id)  # type: ignore[attr-defined]
