"""M3.11 -- the trajectory viewer cannot write, and the database is what says so.

Spec §7.4 calls the viewer read-only. `trajectories.read_only` makes that a property of the
transaction (`SET TRANSACTION READ ONLY`) rather than of the code's current good behaviour, so a
future edit that slipped a write into the viewer would fail at Postgres instead of quietly changing
the record it displays. The control half matters as much as the refusal: the `app` role *may*
insert into `base_env` (§6.1's registration endpoint), so the refusal below is the transaction
mode doing its job, not a missing grant.
"""

from __future__ import annotations

import asyncio

import pytest
from lean_agent_api.trajectories import read_only
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

INSERT = (
    "INSERT INTO base_env (digest, recipe, toolchain_rev, mathlib_rev) "
    "VALUES (:d, '{}', 'v4.33.1', 'viewer-test')"
)


def test_the_viewers_session_refuses_a_write_the_role_could_otherwise_make(
    app_async_database_url: str,
) -> None:
    async def main() -> None:
        engine = create_async_engine(app_async_database_url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)

            # Control: as `app`, outside the viewer's session, the insert is allowed. Rolled back,
            # so nothing is left behind.
            async with sessions() as session:
                await session.execute(text(INSERT), {"d": b"viewer-read-only-control"})
                await session.rollback()

            async with read_only(sessions) as session:
                with pytest.raises(DBAPIError, match="read-only transaction"):
                    await session.execute(text(INSERT), {"d": b"viewer-read-only-refused"})
        finally:
            await engine.dispose()

    asyncio.run(main())
