"""M1.8.4 exit criterion: `VerdictWriter` actually writes a `verdict` row through the real
`leanserv` role's privileges (spec §5.5: `leanserv` is the only writer of verdicts), against a
live PostgreSQL, never a mock -- and a second `write` for the same attempt fails exactly the way
the schema's own primary key says it should.

Local dev / CI setup matches `test_privileges.py`; `admin_engine`, `leanserv_async_database_url`,
and `sealed_obligation` come from `conftest.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import SealedObligation
from lean_agent_core.blobs import INLINE_THRESHOLD, LocalBlobStore
from lean_agent_core.enums import VerdictKind
from lean_agent_serv.verdicts import VerdictInput, VerdictWriter
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
def leanserv_sessionmaker(
    leanserv_async_database_url: str,
) -> Iterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(leanserv_async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


def _insert_attempt(admin_engine: Engine, obligation_id: uuid.UUID, run_id: uuid.UUID) -> uuid.UUID:
    with admin_engine.connect() as conn:
        attempt_id: uuid.UUID = conn.execute(
            text(
                "INSERT INTO attempt (id, obligation_id, run_id, policy_id, policy_config_hash) "
                "VALUES (gen_random_uuid(), :obl, :run, 'p', 'h') RETURNING id"
            ),
            {"obl": str(obligation_id), "run": str(run_id)},
        ).scalar_one()
        conn.commit()
    return attempt_id


def test_write_inserts_a_real_verdict_row(
    admin_engine: Engine,
    leanserv_sessionmaker: async_sessionmaker[AsyncSession],
    sealed_obligation: SealedObligation,
    tmp_path: Path,
) -> None:
    attempt_id = _insert_attempt(admin_engine, sealed_obligation.id, sealed_obligation.run_id)
    writer = VerdictWriter(leanserv_sessionmaker, LocalBlobStore(tmp_path))
    verdict = VerdictInput(
        attempt_id=attempt_id,
        obligation_id=sealed_obligation.id,
        kind=VerdictKind.PROVED,
        link_ok=True,
        replay_ok=True,
        axiom_audit_ok=True,
        elapsed_ms=42,
        toolchain_rev="v4.33.1",
        mathlib_rev="deadbeef",
        sealed_olean_sha_observed=sealed_obligation.sealed_olean_sha,
        axioms=("propext", "Classical.choice"),
        kernels_agreeing=("lean4", "lean4checker"),
        messages=b"no errors",
        proof=b"@sol_1 x y",
    )

    asyncio.run(writer.write(verdict))

    with admin_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT kind, link_ok, replay_ok, axiom_audit_ok, sealed_olean_sha_observed, "
                "axioms, kernels_agreeing, elapsed_ms, cache_hit, toolchain_rev, mathlib_rev "
                "FROM verdict WHERE attempt_id = :att"
            ),
            {"att": str(attempt_id)},
        ).one()
    assert row.kind == VerdictKind.PROVED.value
    assert row.link_ok and row.replay_ok and row.axiom_audit_ok
    assert bytes(row.sealed_olean_sha_observed) == sealed_obligation.sealed_olean_sha
    assert set(row.axioms) == {"propext", "Classical.choice"}
    assert set(row.kernels_agreeing) == {"lean4", "lean4checker"}
    assert row.elapsed_ms == 42
    assert row.cache_hit is False
    assert row.toolchain_rev == "v4.33.1"
    assert row.mathlib_rev == "deadbeef"


def test_write_routes_large_blobs_through_the_cas(
    admin_engine: Engine,
    leanserv_sessionmaker: async_sessionmaker[AsyncSession],
    sealed_obligation: SealedObligation,
    tmp_path: Path,
) -> None:
    """A messages log above the M1.7 inline threshold must round-trip through the blob store, not
    get truncated or stored raw in Postgres (spec §5.3: "Anything above 64 KiB ... never to
    Postgres. Rows hold bytea digests only")."""
    attempt_id = _insert_attempt(admin_engine, sealed_obligation.id, sealed_obligation.run_id)
    blob_store = LocalBlobStore(tmp_path)
    writer = VerdictWriter(leanserv_sessionmaker, blob_store)
    large_messages = b"m" * (INLINE_THRESHOLD + 1)
    verdict = VerdictInput(
        attempt_id=attempt_id,
        obligation_id=sealed_obligation.id,
        kind=VerdictKind.ERRORS,
        link_ok=False,
        replay_ok=True,
        axiom_audit_ok=True,
        elapsed_ms=1,
        toolchain_rev="v4.33.1",
        mathlib_rev="deadbeef",
        messages=large_messages,
    )

    asyncio.run(writer.write(verdict))

    with admin_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT messages_blob FROM verdict WHERE attempt_id = :att"),
            {"att": str(attempt_id)},
        ).scalar_one()
    stored_bytes = bytes(stored)
    # Tagged as a BlobRef (M1.8.4's to_bytea: 0x01 + a 32-byte sha256 digest), not the raw
    # 64 KiB+1 payload -- confirming the column genuinely holds a digest, not inline content.
    assert len(stored_bytes) == 1 + 32
    assert stored_bytes[0:1] == b"\x01"


def test_write_twice_for_the_same_attempt_fails(
    admin_engine: Engine,
    leanserv_sessionmaker: async_sessionmaker[AsyncSession],
    sealed_obligation: SealedObligation,
    tmp_path: Path,
) -> None:
    """`verdict.attempt_id` is the primary key -- at most one verdict per attempt, ever (spec
    §5.3). `VerdictWriter` does not paper over this; a second write for the same attempt must
    fail with the database's own integrity error, not silently overwrite or be swallowed."""
    attempt_id = _insert_attempt(admin_engine, sealed_obligation.id, sealed_obligation.run_id)
    writer = VerdictWriter(leanserv_sessionmaker, LocalBlobStore(tmp_path))
    verdict = VerdictInput(
        attempt_id=attempt_id,
        obligation_id=sealed_obligation.id,
        kind=VerdictKind.PROVED,
        link_ok=True,
        replay_ok=True,
        axiom_audit_ok=True,
        elapsed_ms=1,
        toolchain_rev="v4.33.1",
        mathlib_rev="deadbeef",
    )

    async def run() -> None:
        await writer.write(verdict)
        await writer.write(verdict)

    with pytest.raises(IntegrityError):
        asyncio.run(run())
