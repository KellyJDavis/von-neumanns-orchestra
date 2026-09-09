"""Internal FastAPI surface for `leanserv` (spec §6.2): `/v1/check`, `/v1/check_batch`,
`/v1/health` -- wiring `pool.py` (M1.8.3), `cache.py`, and `verdicts.py` (M1.8.4) together into
the HTTP API spec's own worker-model table describes.

Scope note -- `/v1/seal`, `/v1/link`, `/v1/replay`, `/v1/decompose`, and `/v1/base-env/materialize`
are **not** built here. `LeanKernel.Serve`'s wire protocol (M1.8.1) only knows one request kind,
`check`: it has no seal/link/replay/decompose request shape to dispatch an HTTP route to yet, and
a route with nothing real underneath it is exactly the half-finished surface this project avoids.
`/v1/link` is the endpoint that most wants `VerdictWriter` as a caller (spec: "then replay and
audit; writes the verdict row") -- it stays unwired for the same reason, and is the natural next
step once `Serve.lean` grows a `link` request kind to go with it.

`create_app` is a factory, not a module-level `app` object: `pool`/`cache`/`base_env_sessionmaker`
are real, expensive, stateful resources (a process pool, a database connection pool) that a test
or a real entry point must construct -- this module never constructs them itself. It does,
however, own shutting `pool` down, via FastAPI's own `lifespan` -- see `create_app`'s own note on
why that has to happen there rather than in whatever code called `create_app`.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, HTTPException
from lean_agent_core.enums import VerdictKind
from lean_agent_core.orm import BaseEnv
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lean_agent_serv.cache import CachedCheck, VerificationCacheStore, compute_cache_key
from lean_agent_serv.pool import LeanReplPool
from lean_agent_serv.repl import DEFAULT_TIMEOUT_MS, ReplCrashed, ReplTimeout, ReplWorker


class CheckRequest(BaseModel):
    base_env_digest: str  # hex-encoded sha256, matching base_env.digest
    body: str
    timeout_ms: int = DEFAULT_TIMEOUT_MS


class CheckResponse(BaseModel):
    """`kind` reuses `VerdictKind` for a bare check's outcome, not just a verdict's -- the same
    classification a proof attempt gets (`PROVED`/`ERRORS`/`TIMEOUT`/`INFRA_ERROR`) applies
    unchanged to "did this declaration elaborate cleanly": `ok=True` with no errors is `PROVED`,
    a genuine elaboration error is `ERRORS`, a wallclock timeout is `TIMEOUT` (`ReplTimeout`), and
    any crash (`ReplExited`/`ReplProtocolError`) is `INFRA_ERROR` -- spec's own principle that an
    infra failure is not evidence about the content, applied one layer down from the obligation
    state machine to a single check. `REFUTED` and `OOM` are never produced here: refutation isn't
    what a bare check does, and this module has no reliable way to distinguish an OOM kill from
    any other external kill (see `ReplWorker`'s own crash-taxonomy notes).
    """

    kind: VerdictKind
    ok: bool
    diagnostics: list[str]
    cache_hit: bool
    elapsed_ms: int


class CheckBatchRequest(BaseModel):
    base_env_digest: str
    bodies: list[str]
    timeout_ms: int = DEFAULT_TIMEOUT_MS


class CheckBatchResponse(BaseModel):
    results: list[CheckResponse]


class HealthResponse(BaseModel):
    idle_workers: int
    busy_workers: int
    warm_by_base_env: dict[str, int]
    hit_rate: float


@dataclasses.dataclass(frozen=True)
class _ResolvedBaseEnv:
    imports: tuple[str, ...]
    toolchain_rev: str
    mathlib_rev: str


def _parse_digest(hex_digest: str) -> bytes:
    try:
        return bytes.fromhex(hex_digest)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"base_env_digest is not valid hex: {exc}"
        ) from exc


async def _resolve_base_env(
    session_factory: async_sessionmaker[AsyncSession], digest: bytes
) -> _ResolvedBaseEnv | None:
    async with session_factory() as session:
        row = (
            await session.execute(select(BaseEnv).where(BaseEnv.digest == digest))
        ).scalar_one_or_none()
    if row is None:
        return None
    imports = row.recipe.get("imports", [])
    return _ResolvedBaseEnv(
        imports=tuple(imports), toolchain_rev=row.toolchain_rev, mathlib_rev=row.mathlib_rev
    )


async def _cache_hit_response(
    cache: VerificationCacheStore, cache_key: bytes
) -> CheckResponse | None:
    cached = await cache.get(cache_key)
    if cached is None:
        return None
    # Diagnostics round-trip as a JSON array inside CachedCheck.messages, not as the raw
    # newline-joined text a naive join/split would produce -- a Lean diagnostic can itself
    # contain newlines, which would make splitting on "\n" lossy and ambiguous.
    diagnostics = tuple(json.loads(cached.messages)) if cached.messages else ()
    return CheckResponse(
        kind=cached.kind,
        ok=cached.kind == VerdictKind.PROVED,
        diagnostics=list(diagnostics),
        cache_hit=True,
        elapsed_ms=cached.elapsed_ms,
    )


async def _execute_and_classify(
    worker: ReplWorker,
    cache: VerificationCacheStore,
    cache_key: bytes,
    body: str,
    timeout_ms: int,
    toolchain_rev: str,
    mathlib_rev: str,
) -> CheckResponse:
    started = time.monotonic()
    try:
        result = await worker.check(body, timeout_ms=timeout_ms)
    except ReplTimeout:
        # A timeout is cached deliberately -- spec's verification_cache stores `kind` as
        # `verdict_kind`, which includes `timeout` as a first-class, reproducible outcome, not
        # something to treat as transient. `INFRA_ERROR` below is the one kind that is never
        # cached, precisely because it is *not* reproducible/informative about the content.
        elapsed_ms = int((time.monotonic() - started) * 1000)
        await cache.put(
            cache_key,
            CachedCheck(
                kind=VerdictKind.TIMEOUT,
                axioms=None,
                messages=None,
                infotree=None,
                elapsed_ms=elapsed_ms,
                toolchain_rev=toolchain_rev,
                mathlib_rev=mathlib_rev,
            ),
        )
        return CheckResponse(
            kind=VerdictKind.TIMEOUT,
            ok=False,
            diagnostics=[],
            cache_hit=False,
            elapsed_ms=elapsed_ms,
        )
    except ReplCrashed as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return CheckResponse(
            kind=VerdictKind.INFRA_ERROR,
            ok=False,
            diagnostics=[str(exc)],
            cache_hit=False,
            elapsed_ms=elapsed_ms,
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    kind = VerdictKind.PROVED if result.ok else VerdictKind.ERRORS
    await cache.put(
        cache_key,
        CachedCheck(
            kind=kind,
            axioms=None,
            messages=json.dumps(list(result.diagnostics)).encode(),
            infotree=None,
            elapsed_ms=elapsed_ms,
            toolchain_rev=toolchain_rev,
            mathlib_rev=mathlib_rev,
        ),
    )
    return CheckResponse(
        kind=kind,
        ok=result.ok,
        diagnostics=list(result.diagnostics),
        cache_hit=False,
        elapsed_ms=elapsed_ms,
    )


async def _run_check(
    pool: LeanReplPool,
    cache: VerificationCacheStore,
    base_env_sessionmaker: async_sessionmaker[AsyncSession],
    req: CheckRequest,
) -> CheckResponse:
    base_env_digest = _parse_digest(req.base_env_digest)
    # Content-addressed: a cache hit needs only the digest bytes, never a base_env lookup -- so
    # the (comparatively expensive) database round trip to resolve imports is skipped entirely
    # whenever the answer is already known.
    cache_key = compute_cache_key(base_env_digest, req.body, {})
    hit = await _cache_hit_response(cache, cache_key)
    if hit is not None:
        return hit

    base_env = await _resolve_base_env(base_env_sessionmaker, base_env_digest)
    if base_env is None:
        raise HTTPException(
            status_code=404, detail=f"no base_env with digest {req.base_env_digest!r}"
        )

    async with pool.worker(req.base_env_digest, base_env.imports) as worker:
        return await _execute_and_classify(
            worker,
            cache,
            cache_key,
            req.body,
            req.timeout_ms,
            base_env.toolchain_rev,
            base_env.mathlib_rev,
        )


async def _run_check_batch(
    pool: LeanReplPool,
    cache: VerificationCacheStore,
    base_env_sessionmaker: async_sessionmaker[AsyncSession],
    req: CheckBatchRequest,
) -> CheckBatchResponse:
    base_env_digest = _parse_digest(req.base_env_digest)
    cache_keys = [compute_cache_key(base_env_digest, body, {}) for body in req.bodies]
    results: list[CheckResponse | None] = [await _cache_hit_response(cache, k) for k in cache_keys]

    miss_indices = [i for i, r in enumerate(results) if r is None]
    if miss_indices:
        # "N bodies, one base env; amortizes worker acquisition" (spec §6.2) -- one worker is
        # acquired for every cache miss in this batch, not one per body. If the worker crashes
        # partway through, `ReplWorker.check` on the now-dead worker raises immediately for every
        # remaining item without attempting to use it, so each simply comes back INFRA_ERROR
        # rather than the batch aborting or silently skipping the rest.
        base_env = await _resolve_base_env(base_env_sessionmaker, base_env_digest)
        if base_env is None:
            raise HTTPException(
                status_code=404, detail=f"no base_env with digest {req.base_env_digest!r}"
            )
        async with pool.worker(req.base_env_digest, base_env.imports) as worker:
            for i in miss_indices:
                results[i] = await _execute_and_classify(
                    worker,
                    cache,
                    cache_keys[i],
                    req.bodies[i],
                    req.timeout_ms,
                    base_env.toolchain_rev,
                    base_env.mathlib_rev,
                )

    # Every index was filled above: `_cache_hit_response` fills hits, and `miss_indices` --
    # computed from exactly the `None` entries -- covers every remaining one.
    return CheckBatchResponse(results=cast("list[CheckResponse]", results))


def create_app(
    pool: LeanReplPool,
    cache: VerificationCacheStore,
    base_env_sessionmaker: async_sessionmaker[AsyncSession],
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Closing `pool` here, on ASGI shutdown, rather than leaving it to whatever code
        # constructed the app (a test fixture, a real entry point) is not optional plumbing: an
        # ASGI server (uvicorn, or `TestClient`'s own internal portal) runs the whole app -- every
        # route handler's `await`s, and therefore every `ReplWorker` subprocess `pool` spawned --
        # on *its own* event loop. Closing the pool afterward from a *different* loop (e.g. a
        # caller's own separate `asyncio.run(pool.aclose())`) hits `asyncio.subprocess`'s version
        # of the "Future attached to a different loop" error M1.8.4 already found for asyncpg --
        # and `LeanReplPool.aclose()`'s `asyncio.gather(..., return_exceptions=True)` swallows it
        # silently, so every worker was actually left running, not killed. Confirmed empirically:
        # this was a real, reproducible leak (`ps aux` showing live `leankernel` processes for
        # several seconds after a full `tests/leanserv/test_api.py` run had already exited) before
        # moving cleanup into `lifespan`, which runs in the same loop the workers were spawned in.
        yield
        await pool.aclose()

    app = FastAPI(
        title="leanserv",
        description="Internal Lean Execution Service (spec §6.2)",
        lifespan=lifespan,
    )

    @app.post("/v1/check", response_model=CheckResponse)
    async def check(req: CheckRequest) -> CheckResponse:
        return await _run_check(pool, cache, base_env_sessionmaker, req)

    @app.post("/v1/check_batch", response_model=CheckBatchResponse)
    async def check_batch(req: CheckBatchRequest) -> CheckBatchResponse:
        return await _run_check_batch(pool, cache, base_env_sessionmaker, req)

    @app.get("/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        pool_health = await pool.health()
        return HealthResponse(**dataclasses.asdict(pool_health))

    return app
