"""M1.8.5/M2.1.1/M2.1.2 exit criterion: the FastAPI surface (`/v1/check`, `/v1/check_batch`,
`/v1/seal`, `/v1/link`, `/v1/health`) wired to real infrastructure throughout -- a genuinely
spawned `leankernel serve` process (M1.8.1/M1.8.2) via a real `LeanReplPool` (M1.8.3), and a live
Postgres connected as the real `leanserv` role for the cache (M1.8.4), the `base_env`/`obligation`
lookups, and the `verdict` rows `/v1/link` writes -- never mocked.

`/v1/link` additionally needs a *materialized* sealed bundle: Link requires the goal to be a
genuinely imported constant, which an in-session `seal` result can never be. `materialized_bundle`
below compiles one for real with `lake env lean` and puts its directory on the pool's
`bundle_root`, which is exactly what M2.7's materialization will do in production.

This suite needs *both* the built `leankernel` exe and a live Postgres with `deploy/grants.sql`
applied -- unlike `test_repl.py`/`test_pool.py` (Lean only) or `tests/db/`'s suites (Postgres
only). It lives here, under `tests/leanserv/`, and CI's `lean` job grew a Postgres service
container specifically so both prerequisites are available in the one job that already pays for
the (expensive) Lean toolchain/Mathlib setup -- see CLAUDE.md and `.github/workflows/ci.yml`.

No pytest-asyncio: `TestClient` manages its own event loop internally, so these are plain sync
`def test_...` functions calling `client.post`/`client.get` directly, same as any other requests-
style HTTP test -- no `asyncio.run` needed here at all, unlike `test_repl.py`/`test_pool.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import MaterializedBundle
from fastapi.testclient import TestClient
from lean_agent_core.blobs import LocalBlobStore
from lean_agent_serv.api import create_app
from lean_agent_serv.cache import VerificationCacheStore
from lean_agent_serv.pool import LeanReplPool, PoolConfig
from lean_agent_serv.verdicts import VerdictWriter
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
def registered_base_env(admin_engine: Engine) -> Iterator[str]:
    """A real, committed `base_env` row with a Mathlib-free (`Init`-only) recipe, for speed --
    returns its digest as a hex string, the same encoding `/v1/check` expects on the wire.
    """
    digest = f"api-test-{uuid.uuid4()}".encode()
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO base_env (digest, recipe, toolchain_rev, mathlib_rev) "
                "VALUES (:digest, '{\"imports\": [\"Init\"]}', 'v4.33.1', 'deadbeef')"
            ),
            {"digest": digest},
        )
        conn.commit()
    yield digest.hex()
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM base_env WHERE digest = :digest"), {"digest": digest})
        conn.commit()


@pytest.fixture(autouse=True)
def _cleanup_verification_cache(admin_engine: Engine) -> Iterator[None]:
    yield
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM verification_cache"))
        conn.commit()


@pytest.fixture
def client(
    lake_project_dir: Path,
    leanserv_async_database_url: str,
    tmp_path: Path,
    materialized_bundle: MaterializedBundle,
) -> Iterator[TestClient]:
    engine = create_async_engine(leanserv_async_database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    blob_store = LocalBlobStore(tmp_path)
    pool = LeanReplPool(
        lake_project_dir,
        PoolConfig(max_total_workers=4, bundle_root=materialized_bundle.root),
    )
    cache = VerificationCacheStore(sessionmaker, blob_store)
    app = create_app(pool, cache, VerdictWriter(sessionmaker, blob_store), sessionmaker)

    with TestClient(app) as c:
        yield c

    # Not `asyncio.run(pool.aclose())` here: `create_app`'s own `lifespan` already closed `pool`
    # during `TestClient`'s `__exit__` above, in the same event loop the workers were spawned in
    # (see api.py's `lifespan` docstring for why a separate loop here would silently leak every
    # worker). `engine.dispose()` from a different loop than the one that used it is still fine
    # empirically (confirmed: Postgres's own connection count returns to baseline either way).
    asyncio.run(engine.dispose())


def test_check_ok(client: TestClient, registered_base_env: str) -> None:
    response = client.post(
        "/v1/check", json={"base_env_digest": registered_base_env, "body": "def foo : Nat := 5"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "proved"
    assert body["ok"] is True
    assert body["diagnostics"] == []
    assert body["cache_hit"] is False


def test_check_type_error(client: TestClient, registered_base_env: str) -> None:
    response = client.post(
        "/v1/check",
        json={"base_env_digest": registered_base_env, "body": "def bad : Nat := true"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "errors"
    assert body["ok"] is False
    assert body["diagnostics"]


def test_check_second_call_is_a_cache_hit(client: TestClient, registered_base_env: str) -> None:
    req = {"base_env_digest": registered_base_env, "body": "def foo : Nat := 5"}
    first = client.post("/v1/check", json=req).json()
    second = client.post("/v1/check", json=req).json()

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert second["kind"] == first["kind"]
    assert second["ok"] == first["ok"]


def test_check_unknown_base_env_is_404(client: TestClient) -> None:
    response = client.post(
        "/v1/check", json={"base_env_digest": "ab" * 32, "body": "def foo : Nat := 5"}
    )
    assert response.status_code == 404


def test_check_invalid_hex_digest_is_400(client: TestClient) -> None:
    response = client.post(
        "/v1/check", json={"base_env_digest": "not-hex-at-all", "body": "def foo : Nat := 5"}
    )
    assert response.status_code == 400


def test_check_timeout_reports_infra_taxonomy(client: TestClient, registered_base_env: str) -> None:
    response = client.post(
        "/v1/check",
        json={
            "base_env_digest": registered_base_env,
            "body": "partial def spin : IO Unit := spin\n#eval spin",
            "timeout_ms": 1000,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "timeout"
    assert body["ok"] is False


def test_check_batch_amortizes_and_mixes_hits_and_misses(
    client: TestClient, registered_base_env: str
) -> None:
    warm_body = "def already_cached : Nat := 1"
    client.post("/v1/check", json={"base_env_digest": registered_base_env, "body": warm_body})

    response = client.post(
        "/v1/check_batch",
        json={
            "base_env_digest": registered_base_env,
            "bodies": [warm_body, "def freshly_checked : Nat := 2"],
        },
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 2
    assert results[0]["cache_hit"] is True
    assert results[0]["ok"] is True
    assert results[1]["cache_hit"] is False
    assert results[1]["ok"] is True


def test_seal_creates_a_bundle_for_the_goals_that_elaborate(
    client: TestClient, registered_base_env: str
) -> None:
    """Spec §6.1's headline seal behaviour, end to end: a submission whose goals do not all
    elaborate still seals the ones that do, and reports the one that does not."""
    response = client.post(
        "/v1/seal",
        json={
            "base_env_digest": registered_base_env,
            "goals": [
                {"name": "G_ok", "statement": "∀ n : Nat, n + 0 = n"},
                {"name": "G_bad", "statement": "SomeUndefinedThing"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    ok_goal, bad_goal = body["goals"]

    assert ok_goal["ok"] is True
    assert ok_goal["decl_name"] == "LeanAgent.Goals.G_ok"
    assert ok_goal["goal_src"] == "∀ n : Nat, n + 0 = n"
    assert ok_goal["diagnostics"] == []

    assert bad_goal["ok"] is False
    assert bad_goal["diagnostics"]

    # The bundle is compiled out of band with nothing re-verifying it, so the goal that failed to
    # seal must not be in the source that gets compiled -- only the reports mention it.
    assert "G_ok" in body["bundle_source"]
    assert "SomeUndefinedThing" not in body["bundle_source"]
    assert "import Init" in body["bundle_source"]
    assert "set_option autoImplicit false" in body["bundle_source"]


def test_seal_bundle_digest_addresses_the_returned_source(
    client: TestClient, registered_base_env: str
) -> None:
    response = client.post(
        "/v1/seal",
        json={
            "base_env_digest": registered_base_env,
            "goals": [{"name": "G_digest", "statement": "True"}],
        },
    )
    body = response.json()
    assert body["ok"] is True
    assert body["bundle_digest"] == hashlib.sha256(body["bundle_source"].encode()).hexdigest()


def test_seal_goal_digest_ignores_the_declaration_name(
    client: TestClient, registered_base_env: str
) -> None:
    """The same statement under two different names is the same goal. This is what makes spec
    §5.2's cycle guard ("a child's `goal_digest` may not equal any ancestor's") able to fire at
    all -- a digest that folded in the generated declaration name would be unique per obligation
    and the guard would silently never match.
    """
    response = client.post(
        "/v1/seal",
        json={
            "base_env_digest": registered_base_env,
            "goals": [
                {"name": "G_first", "statement": "∀ n : Nat, n + 0 = n"},
                {"name": "G_second", "statement": "∀ n : Nat, n + 0 = n"},
                {"name": "G_other", "statement": "True"},
            ],
        },
    )
    first, second, other = response.json()["goals"]
    assert first["decl_name"] != second["decl_name"]
    assert first["goal_digest"] == second["goal_digest"]
    assert other["goal_digest"] != first["goal_digest"]


def test_seal_rejects_a_statement_that_declares_anything_else(
    client: TestClient, registered_base_env: str
) -> None:
    """`statement` is spliced into generated source, so it is an injection site -- a statement
    that closes the `def` early and appends its own commands must not end up in a bundle that is
    supposed to be an environment the agent cannot influence (spec §1.1)."""
    response = client.post(
        "/v1/seal",
        json={
            "base_env_digest": registered_base_env,
            "goals": [
                {
                    "name": "G_esc",
                    "statement": (
                        "True\nend LeanAgent.Goals\ndef Evil : Nat := 0\nnamespace LeanAgent.Goals"
                    ),
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["goals"][0]["ok"] is False
    assert "Evil" not in body["bundle_source"]


def test_seal_unknown_base_env_is_404(client: TestClient) -> None:
    response = client.post(
        "/v1/seal",
        json={"base_env_digest": "ab" * 32, "goals": [{"name": "G", "statement": "True"}]},
    )
    assert response.status_code == 404


@dataclass(frozen=True)
class LinkableObligation:
    """A committed run/obligation/attempt trio whose `sealed_olean_sha` is the *real* digest of
    the compiled bundle, so `mark_proved`'s seal-integrity comparison has something true to
    compare against rather than a placeholder."""

    obligation_id: uuid.UUID
    attempt_id: uuid.UUID
    run_id: uuid.UUID


@pytest.fixture
def linkable(
    admin_engine: Engine, registered_base_env: str, materialized_bundle: MaterializedBundle
) -> Iterator[LinkableObligation]:
    base_env_digest = bytes.fromhex(registered_base_env)
    run_id, obligation_id, attempt_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, tenant_id, base_env_digest, status, manifest, manifest_hash)"
                " VALUES (:id, :tenant, :base_env, 'running', '{}', :mh)"
            ),
            {
                "id": run_id,
                "tenant": uuid.uuid4(),
                "base_env": base_env_digest,
                "mh": b"manifest-hash",
            },
        )
        conn.execute(
            text(
                "INSERT INTO obligation (id, run_id, base_env_digest, goal_digest, "
                "sealed_olean_sha, goal_src, decl_name) VALUES (:id, :run, :base_env, :gd, "
                ":sealed, :src, :decl)"
            ),
            {
                "id": obligation_id,
                "run": run_id,
                "base_env": base_env_digest,
                "gd": b"goal-digest",
                "sealed": materialized_bundle.olean_digest,
                "src": "∀ n : Nat, n + 0 = n",
                "decl": "LeanAgent.Goals.G_add_zero",
            },
        )
        conn.execute(
            text(
                "INSERT INTO attempt (id, obligation_id, run_id, policy_id, policy_config_hash) "
                "VALUES (:id, :obl, :run, 'null-agent', 'h')"
            ),
            {"id": attempt_id, "obl": obligation_id, "run": run_id},
        )
        conn.commit()
    yield LinkableObligation(obligation_id=obligation_id, attempt_id=attempt_id, run_id=run_id)
    with admin_engine.connect() as conn:
        conn.execute(text("DELETE FROM run WHERE id = :id"), {"id": run_id})
        conn.commit()


def _link_body(
    linkable: LinkableObligation,
    base_env: str,
    bundle: MaterializedBundle,
    *,
    development: str,
    goal: str = "LeanAgent.Goals.G_add_zero",
    entry: str = "LeanAgent.Sol.sol",
) -> dict[str, object]:
    return {
        "attempt_id": str(linkable.attempt_id),
        "obligation_id": str(linkable.obligation_id),
        "base_env_digest": base_env,
        "bundle_sha": bundle.sha,
        "goal": goal,
        "entry": entry,
        "development": development,
    }


def _stored_verdict(admin_engine: Engine, attempt_id: uuid.UUID) -> dict[str, object]:
    with admin_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT kind::text, link_ok, replay_ok, axiom_audit_ok, axioms, "
                "sealed_olean_sha_observed FROM verdict WHERE attempt_id = :id"
            ),
            {"id": attempt_id},
        ).one()
    return dict(zip(row._fields, row, strict=True))


def test_link_accepts_a_genuine_proof_and_writes_the_verdict(
    client: TestClient,
    admin_engine: Engine,
    registered_base_env: str,
    materialized_bundle: MaterializedBundle,
    linkable: LinkableObligation,
) -> None:
    """The whole acceptance path over HTTP, ending in the one artifact that matters downstream:
    a `verdict` row only `leanserv` can write (spec §5.5/§6.4)."""
    response = client.post(
        "/v1/link",
        json=_link_body(
            linkable,
            registered_base_env,
            materialized_bundle,
            development=(
                "namespace LeanAgent.Sol\n"
                "theorem sol : ∀ n : Nat, n + 0 = n := fun _ => rfl\n"
                "end LeanAgent.Sol"
            ),
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "proved"
    assert body["link_ok"] is True
    assert body["replay_ok"] is True
    assert body["axiom_audit_ok"] is True
    assert body["axioms"] == []
    # Never populated until multi-kernel replay exists -- an empty list is the honest answer.
    assert body["kernels_agreeing"] == []

    stored = _stored_verdict(admin_engine, linkable.attempt_id)
    assert stored["kind"] == "proved"
    assert (stored["link_ok"], stored["replay_ok"], stored["axiom_audit_ok"]) == (True, True, True)
    # Observed by hashing the .olean the *worker* said it resolved the goal from, not echoed back
    # from the request -- this is what `mark_proved` compares against `obligation.sealed_olean_sha`.
    assert stored["sealed_olean_sha_observed"] == materialized_bundle.olean_digest
    # And it is the digest of the file at the path leanserv's own `bundle_sha` -> path convention
    # predicts, which is what pins that convention to the module name Lean's search path resolved.
    expected_olean = (
        materialized_bundle.root / "LeanAgent" / "Goals" / f"Bundle_{materialized_bundle.sha}.olean"
    )
    assert (
        stored["sealed_olean_sha_observed"] == hashlib.sha256(expected_olean.read_bytes()).digest()
    )


def test_link_rejects_a_weaker_statement(
    client: TestClient,
    registered_base_env: str,
    materialized_bundle: MaterializedBundle,
    linkable: LinkableObligation,
) -> None:
    """Spec §4.2's core claim, end to end: the agent never writes the statement, so a proof of
    something strictly weaker (an existential where the goal is universal) cannot link -- rejected
    by kernel type-checking of the constructed declaration, not by any inspection leanserv does.
    """
    response = client.post(
        "/v1/link",
        json=_link_body(
            linkable,
            registered_base_env,
            materialized_bundle,
            development=(
                "namespace LeanAgent.Sol\n"
                "def sol : ∃ n : Nat, n + 0 = n := ⟨0, rfl⟩\n"
                "end LeanAgent.Sol"
            ),
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "errors"
    assert body["link_ok"] is False
    # Replay still passes: the agent's own declaration is internally kernel-sound, it simply
    # isn't a proof of this goal. The two checks answer different questions (CLAUDE.md, M1.2/M1.3).
    assert body["replay_ok"] is True
    assert any("type mismatch" in d for d in body["diagnostics"])


def test_link_rejects_a_weaker_statement_with_kernel_checking_poisoned(
    client: TestClient,
    registered_base_env: str,
    materialized_bundle: MaterializedBundle,
    linkable: LinkableObligation,
) -> None:
    """Gate 4 over HTTP: the same weakened proof, with the submission's own `set_option
    debug.skipKernelTC true` trying to disable the check that catches it. Link builds its own
    options rather than reading the session's, so the poisoning changes nothing."""
    response = client.post(
        "/v1/link",
        json=_link_body(
            linkable,
            registered_base_env,
            materialized_bundle,
            development=(
                "set_option debug.skipKernelTC true\n"
                "namespace LeanAgent.Sol\n"
                "def sol : ∃ n : Nat, n + 0 = n := ⟨0, rfl⟩\n"
                "end LeanAgent.Sol"
            ),
        ),
    )
    assert response.json()["link_ok"] is False


def test_link_reports_a_sorry_proof_as_an_audit_failure_not_a_link_failure(
    client: TestClient,
    admin_engine: Engine,
    registered_base_env: str,
    materialized_bundle: MaterializedBundle,
    linkable: LinkableObligation,
) -> None:
    """The case that makes `link_ok` and `axiom_audit_ok` worth separating: a `sorry`-backed term
    genuinely *does* have the goal's type, so the kernel accepts it; what rejects it is the run's
    axiom allowlist (spec §4.4), which does not include `sorryAx` unless `run.allow_sorry`."""
    response = client.post(
        "/v1/link",
        json=_link_body(
            linkable,
            registered_base_env,
            materialized_bundle,
            development=(
                "namespace LeanAgent.Sol\n"
                "theorem sol : ∀ n : Nat, n + 0 = n := sorry\n"
                "end LeanAgent.Sol"
            ),
        ),
    )
    body = response.json()
    assert body["kind"] == "errors"
    assert body["link_ok"] is True
    assert body["axiom_audit_ok"] is False
    assert body["axioms"] == ["sorryAx"]
    assert _stored_verdict(admin_engine, linkable.attempt_id)["axioms"] == ["sorryAx"]


def test_link_honours_run_allow_sorry(
    client: TestClient,
    admin_engine: Engine,
    registered_base_env: str,
    materialized_bundle: MaterializedBundle,
    linkable: LinkableObligation,
) -> None:
    """The allowlist comes from the *run*, never the request -- so flipping `run.allow_sorry` is
    what changes the outcome, and no caller can grant itself `sorryAx`."""
    with admin_engine.connect() as conn:
        conn.execute(
            text("UPDATE run SET allow_sorry = true WHERE id = :id"), {"id": linkable.run_id}
        )
        conn.commit()

    response = client.post(
        "/v1/link",
        json=_link_body(
            linkable,
            registered_base_env,
            materialized_bundle,
            development=(
                "namespace LeanAgent.Sol\n"
                "theorem sol : ∀ n : Nat, n + 0 = n := sorry\n"
                "end LeanAgent.Sol"
            ),
        ),
    )
    body = response.json()
    assert body["kind"] == "proved"
    assert body["axiom_audit_ok"] is True
    assert body["axioms"] == ["sorryAx"]


def test_link_reports_a_missing_entry_point(
    client: TestClient,
    registered_base_env: str,
    materialized_bundle: MaterializedBundle,
    linkable: LinkableObligation,
) -> None:
    """Spec §4.2: "a proof under a different name reports a name mismatch, not a proof failure"."""
    response = client.post(
        "/v1/link",
        json=_link_body(
            linkable,
            registered_base_env,
            materialized_bundle,
            development=(
                "namespace LeanAgent.Sol\n"
                "theorem differently_named : ∀ n : Nat, n + 0 = n := fun _ => rfl\n"
                "end LeanAgent.Sol"
            ),
        ),
    )
    body = response.json()
    assert body["link_ok"] is False
    assert any("entry point missing" in d for d in body["diagnostics"])


def test_link_reports_a_development_that_does_not_elaborate(
    client: TestClient,
    registered_base_env: str,
    materialized_bundle: MaterializedBundle,
    linkable: LinkableObligation,
) -> None:
    response = client.post(
        "/v1/link",
        json=_link_body(
            linkable,
            registered_base_env,
            materialized_bundle,
            development=(
                "namespace LeanAgent.Sol\n"
                "theorem sol : ∀ n : Nat, n + 0 = n := NoSuchThing\n"
                "end LeanAgent.Sol"
            ),
        ),
    )
    body = response.json()
    assert body["kind"] == "errors"
    assert body["link_ok"] is False
    assert body["replay_ok"] is False
    assert any("NoSuchThing" in d for d in body["diagnostics"])


def test_link_links_a_universe_polymorphic_data_goal(
    client: TestClient,
    registered_base_env: str,
    materialized_bundle: MaterializedBundle,
    linkable: LinkableObligation,
) -> None:
    """Link's `defnDecl` branch (a `Sort`-valued, non-`Prop` goal) over HTTP -- solved by
    *inhabiting* the goal's value, not by restating its shape (CLAUDE.md's M1.2 note)."""
    response = client.post(
        "/v1/link",
        json=_link_body(
            linkable,
            registered_base_env,
            materialized_bundle,
            goal="LeanAgent.Goals.G_poly",
            development=(
                "namespace LeanAgent.Sol\ndef sol.{v} : PUnit.{v} := PUnit.unit\nend LeanAgent.Sol"
            ),
        ),
    )
    body = response.json()
    assert body["kind"] == "proved"
    assert body["link_ok"] is True


def test_link_unmaterialized_bundle_is_a_404_not_an_infra_error(
    client: TestClient,
    admin_engine: Engine,
    registered_base_env: str,
    materialized_bundle: MaterializedBundle,
    linkable: LinkableObligation,
) -> None:
    """A bundle nobody built must fail as a caller error, not as retryable infrastructure trouble.

    Without the pre-check this really did come back as `infra_error` with the diagnostic "stdout
    closed (process exited) while awaiting response" -- the worker died inside `importModules`,
    which is a genuine crash but the wrong *taxonomy*: `infra_error` is unbudgeted and invites
    retry, and no retry will conjure a bundle. It must also leave no `verdict` row, since nothing
    about the submission was ever judged.
    """
    response = client.post(
        "/v1/link",
        json={
            **_link_body(
                linkable,
                registered_base_env,
                materialized_bundle,
                development="namespace LeanAgent.Sol\ntheorem sol : True := trivial\nend LeanAgent.Sol",
            ),
            "bundle_sha": "0" * 64,
        },
    )
    assert response.status_code == 404
    with admin_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT count(*) FROM verdict WHERE attempt_id = :id"),
            {"id": linkable.attempt_id},
        ).scalar_one()
    assert stored == 0


def test_link_paranoid_is_refused_rather_than_faked(
    client: TestClient,
    registered_base_env: str,
    materialized_bundle: MaterializedBundle,
    linkable: LinkableObligation,
) -> None:
    response = client.post(
        "/v1/link",
        json={
            **_link_body(
                linkable,
                registered_base_env,
                materialized_bundle,
                development="namespace LeanAgent.Sol\ntheorem sol : True := trivial\nend LeanAgent.Sol",
            ),
            "paranoid": True,
        },
    )
    assert response.status_code == 400


def test_link_unknown_obligation_is_404(
    client: TestClient, registered_base_env: str, materialized_bundle: MaterializedBundle
) -> None:
    response = client.post(
        "/v1/link",
        json={
            "attempt_id": str(uuid.uuid4()),
            "obligation_id": str(uuid.uuid4()),
            "base_env_digest": registered_base_env,
            "bundle_sha": materialized_bundle.sha,
            "goal": "LeanAgent.Goals.G_add_zero",
            "entry": "LeanAgent.Sol.sol",
            "development": "namespace LeanAgent.Sol\ntheorem sol : True := trivial\nend LeanAgent.Sol",
        },
    )
    assert response.status_code == 404


def test_health_reports_pool_shape(client: TestClient, registered_base_env: str) -> None:
    client.post(
        "/v1/check", json={"base_env_digest": registered_base_env, "body": "def foo : Nat := 5"}
    )

    response = client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["idle_workers"] >= 1
    assert body["busy_workers"] == 0
    assert 0.0 <= body["hit_rate"] <= 1.0
    assert isinstance(body["warm_by_base_env"], dict)
