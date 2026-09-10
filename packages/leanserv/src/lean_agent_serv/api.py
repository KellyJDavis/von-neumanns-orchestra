"""Internal FastAPI surface for `leanserv` (spec §6.2): `/v1/check`, `/v1/check_batch`,
`/v1/seal`, `/v1/link`, `/v1/decompose`, `/v1/health` -- wiring `pool.py` (M1.8.3), `cache.py`, and
`verdicts.py` (M1.8.4) together into the HTTP API spec's own worker-model table describes.

`/v1/link` is where this module becomes the *only* writer of `verdict` rows (spec §5.5/§6.4):
workers request a check and observe the outcome, never transcribing it themselves, and
`deploy/grants.sql` enforces that at the database.

Scope note -- `/v1/replay` and `/v1/base-env/materialize` are **not** built here. Standalone
`/v1/replay` has no caller: `/v1/link` already replays as part of the acceptance path, which is
what spec §4.3 actually asks for, and a route with nothing real underneath it is exactly the
half-finished surface this project avoids.

`create_app` is a factory, not a module-level `app` object: `pool`/`cache`/`verdict_writer`/
`base_env_sessionmaker` are real, expensive, stateful resources (a process pool, a database
connection pool) that a test or a real entry point must construct -- this module never constructs
them itself. It does, however, own shutting `pool` down, via FastAPI's own `lifespan` -- see
`create_app`'s own note on why that has to happen there rather than in whatever code called
`create_app`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI, HTTPException
from lean_agent_core.digests import compute_bundle_digest, compute_goal_digest
from lean_agent_core.enums import VerdictKind
from lean_agent_core.orm import BaseEnv, Obligation, Run
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lean_agent_serv.cache import CachedCheck, VerificationCacheStore, compute_cache_key
from lean_agent_serv.pool import LeanReplPool
from lean_agent_serv.repl import (
    DEFAULT_TIMEOUT_MS,
    LinkResult,
    ReplCrashed,
    ReplTimeout,
    ReplWorker,
    SealGoal,
)
from lean_agent_serv.verdicts import VerdictInput, VerdictWriter


class CheckRequest(BaseModel):
    base_env_digest: str  # hex-encoded sha256, matching base_env.digest
    body: str
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    #: Optionally put a materialized sealed bundle on the worker's path, so `body` can name a
    #: sealed goal constant. This is what lets a policy *screen* candidate proofs before committing
    #: its one `/v1/link` -- `verdict.attempt_id` is a primary key, so an attempt gets exactly one
    #: verdict and a fifteen-tactic portfolio cannot spend it fifteen times. Screening on `check`
    #: (cheap, cached, no verdict) and linking once (authoritative, writes the verdict) is what the
    #: two endpoints are for.
    bundle_sha: str | None = None


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


class SealGoalRequest(BaseModel):
    name: str
    statement: str
    #: Declared on the generated `def` (spec §4.1's `def G_<id>.{u_0}`). Needed whenever
    #: `statement` names a universe, which a decomposed subgoal's printed statement routinely
    #: does -- sealing forces `autoImplicit false`, so a free universe name is an error, not
    #: something Lean binds. Empty for the ordinary monomorphic case.
    level_params: list[str] = []


class SealRequest(BaseModel):
    base_env_digest: str
    goals: list[SealGoalRequest]
    timeout_ms: int = DEFAULT_TIMEOUT_MS


class SealedGoalResponse(BaseModel):
    """One goal's seal outcome, in the shape the caller needs to insert an `obligation` row:
    `decl_name`, `goal_src` and `goal_digest` are that table's own columns (spec §5.2).

    `sealed_olean_sha` is deliberately absent. It is the digest of the *compiled* bundle, and spec
    §4.1 produces that `.olean` lazily out of band -- "the hot path never waits on the build
    system" -- so this endpoint cannot know it. `bundle_digest` below names the bundle whose
    compilation will eventually supply it (spec's generated file is `Bundle_<digest>.lean`).
    """

    decl_name: str
    goal_src: str
    goal_digest: str
    level_params: list[str]
    diagnostics: list[str]
    ok: bool


class SealResponse(BaseModel):
    """`goals` is parallel to the request's own `goals`, and `ok` is false if *any* of them failed
    to seal -- but the ones that did seal are still reported as `ok`, which is what spec §6.1's
    "a submission with ten goals of which one does not elaborate creates nine obligations and
    reports the tenth" requires of the caller.

    `bundle_source` contains only the goals that sealed, and `bundle_digest` is its sha256 --
    together they are what an eventual out-of-band build compiles into `Bundle_<digest>.lean`.
    """

    ok: bool
    goals: list[SealedGoalResponse]
    bundle_source: str
    bundle_digest: str
    elapsed_ms: int


class DecomposeRequest(BaseModel):
    base_env_digest: str
    development: str
    timeout_ms: int = DEFAULT_TIMEOUT_MS


class DecomposedLemmaResponse(BaseModel):
    """One extracted subgoal, in the shape `/v1/seal` accepts directly -- `name`, `statement` and
    `level_params` are exactly `SealGoalRequest`'s fields, because sealing each child is what
    happens next (spec §6.3's ingestion: "extract `sorry` sites, seal each site's goal").

    `round_trips` false means the printed statement does not seal back to the `Expr` it came from,
    so it is *not* a faithful stand-in for the abstracted goal. Creating an obligation from one
    would mean proving something other than what the parent's reassembly needs.
    """

    name: str
    statement: str
    level_params: list[str]
    round_trips: bool
    diagnostics: list[str]


class DecomposeResponse(BaseModel):
    """`ok` with no `lemmas` means the development contained no `sorry`; `ok=False` means it did
    not elaborate. `reassembly` is the source with each `sorry` replaced by its child -- running it
    is a full acceptance check against the parent's sealed goal (spec §4.6), which is `/v1/link`'s
    job, not this endpoint's.
    """

    ok: bool
    lemmas: list[DecomposedLemmaResponse]
    reassembly: str
    diagnostics: list[str]
    elapsed_ms: int


class LinkRequest(BaseModel):
    """Spec §6.2's `LinkRequest`. `bundle_sha` names the sealed bundle: spec §4.1 generates it as
    `LeanAgent/Goals/Bundle_<digest>.lean`, so the digest is also the module name the worker
    imports (`_bundle_module_name`) -- there is no separate module field, and no way for a caller
    to point the worker at a bundle other than the one it named.

    `paranoid` (spec's multi-kernel replay) is accepted and must be `False`. This distribution
    ships one kernel implementation, and spec §4.3 is explicit that independence across
    *implementations* is the only thing multi-kernel replay buys -- so honouring the flag today
    would mean replaying twice through the same kernel and reporting two agreeing "kernels", which
    is worse than not offering it.
    """

    attempt_id: uuid.UUID
    obligation_id: uuid.UUID
    base_env_digest: str
    bundle_sha: str
    goal: str
    entry: str
    development: str
    paranoid: bool = False
    timeout_ms: int = DEFAULT_TIMEOUT_MS


class LinkResponse(BaseModel):
    """Spec §6.2's `LinkResponse`, plus `diagnostics`.

    `link_ok` is the kernel's verdict on the constructed declaration alone; `axiom_audit_ok` is
    §4.4's separate question about the trust base. A `sorry`-backed proof is the case that makes
    the split worth having: it links (the term really does have the goal's type) and fails the
    audit, and reporting that as a link failure would describe it wrongly.

    `kernels_agreeing` is always empty until multi-kernel replay exists -- an empty list means "no
    multi-kernel agreement was established", which is the truth, rather than naming the single
    kernel that ran and implying corroboration that did not happen.
    """

    kind: VerdictKind
    link_ok: bool
    replay_ok: bool
    axiom_audit_ok: bool
    axioms: list[str]
    kernels_agreeing: list[str]
    elapsed_ms: int
    cache_hit: bool
    diagnostics: list[str]


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


def _check_worker_key(req: CheckRequest) -> str:
    """A bundle-bearing check needs its own pool key, since its worker's imports differ -- the
    same rule `/v1/link` follows, and the same one warm slot per (base env, bundle) cost."""
    if req.bundle_sha is None:
        return req.base_env_digest
    return f"{req.base_env_digest}+{_bundle_module_name(req.bundle_sha)}"


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
    #
    # `bundle_sha` goes into the key's options, not alongside them as decoration: the same body
    # against two different bundles is two different checks (the sealed constant it names means
    # different things), and a key that ignored it would serve one bundle's answer for another's
    # question -- a wrong *acceptance*, not just a stale one.
    cache_key = compute_cache_key(
        base_env_digest, req.body, {"bundle_sha": req.bundle_sha} if req.bundle_sha else {}
    )
    hit = await _cache_hit_response(cache, cache_key)
    if hit is not None:
        return hit

    base_env = await _resolve_base_env(base_env_sessionmaker, base_env_digest)
    if base_env is None:
        raise HTTPException(
            status_code=404, detail=f"no base_env with digest {req.base_env_digest!r}"
        )

    imports = base_env.imports
    if req.bundle_sha is not None:
        if (
            pool.bundle_root is None
            or not _bundle_olean_path(pool.bundle_root, req.bundle_sha).exists()
        ):
            raise HTTPException(
                status_code=404,
                detail=f"bundle {req.bundle_sha} is not materialized under {pool.bundle_root}",
            )
        imports = (*imports, _bundle_module_name(req.bundle_sha))

    async with pool.worker(_check_worker_key(req), imports) as worker:
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


async def _run_seal(
    pool: LeanReplPool,
    base_env_sessionmaker: async_sessionmaker[AsyncSession],
    req: SealRequest,
) -> SealResponse:
    """Seal a goal bundle in a warm worker (spec §4.1).

    Uncached, unlike `/v1/check`, and deliberately so. `verification_cache` is keyed by
    `verdict_kind` -- it answers "did this declaration check", not "what does this goal
    elaborate to" -- and there is nothing in a `CachedCheck` that could carry back the per-goal
    reports or the bundle source a caller needs here. Sealing is also elaboration-only against an
    already-warm environment, which is the cheap side of the very measurement that motivates warm
    workers at all (spec §4.1: cold ~78% of pipeline time, warm under 1%), so there is little to
    win and a wrong-shaped cache entry to lose.
    """
    base_env_digest = _parse_digest(req.base_env_digest)
    base_env = await _resolve_base_env(base_env_sessionmaker, base_env_digest)
    if base_env is None:
        raise HTTPException(
            status_code=404, detail=f"no base_env with digest {req.base_env_digest!r}"
        )

    started = time.monotonic()
    async with pool.worker(req.base_env_digest, base_env.imports) as worker:
        try:
            result = await worker.seal(
                [
                    SealGoal(
                        name=g.name,
                        statement=g.statement,
                        level_params=tuple(g.level_params),
                    )
                    for g in req.goals
                ],
                timeout_ms=req.timeout_ms,
            )
        except ReplCrashed as exc:
            # 503, not a `SealResponse` with `ok=False`: spec's `infra_error` is a first-class
            # outcome that is explicitly *not* evidence about the content, and `seal_failed` is
            # a statement about the content ("the statement does not elaborate", §4.1). Reporting
            # a crashed worker as a failed seal would tell the caller not to create obligations
            # for goals that were never actually judged.
            raise HTTPException(status_code=503, detail=f"lean worker unusable: {exc}") from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)

    # `req.goals` and `result.goals` are parallel by construction (`Serve.lean` reports one entry
    # per requested goal, in order), so a goal's own statement is what pairs with its report --
    # `SealedGoal.decl` is the *qualified* name and is not the request's `name` field.
    goals = [
        SealedGoalResponse(
            decl_name=report.decl,
            goal_src=requested.statement,
            goal_digest=compute_goal_digest(base_env_digest, requested.statement).hex(),
            level_params=list(report.level_params),
            diagnostics=list(report.diagnostics),
            ok=report.ok,
        )
        for requested, report in zip(req.goals, result.goals, strict=True)
    ]
    return SealResponse(
        ok=result.ok,
        goals=goals,
        bundle_source=result.bundle_source,
        bundle_digest=compute_bundle_digest(result.bundle_source).hex(),
        elapsed_ms=elapsed_ms,
    )


@dataclasses.dataclass(frozen=True)
class _ResolvedObligation:
    sealed_olean_sha: bytes
    axiom_allowlist: tuple[str, ...]


async def _resolve_obligation(
    session_factory: async_sessionmaker[AsyncSession], obligation_id: uuid.UUID
) -> _ResolvedObligation | None:
    """The obligation's own sealed-bundle digest, and its run's axiom allowlist (spec §4.4).

    The allowlist is read here rather than taken from the request for the same reason `verdict`
    rows are written here rather than by workers (spec §5.5/§6.4): the run decides what axioms are
    permitted, and a caller that could pass its own allowlist could permit `sorryAx` for a run that
    forbids it. `run.allow_sorry` is folded in as `sorryAx` because that is exactly what the column
    means -- `auditAxioms` itself applies no special case for `sorry`, by design.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                select(Obligation.sealed_olean_sha, Run.axiom_allowlist, Run.allow_sorry)
                .join(Run, Run.id == Obligation.run_id)
                .where(Obligation.id == obligation_id)
            )
        ).one_or_none()
    if row is None:
        return None
    sealed_olean_sha, allowlist, allow_sorry = row
    axioms = tuple(allowlist) + (("sorryAx",) if allow_sorry else ())
    return _ResolvedObligation(sealed_olean_sha=sealed_olean_sha, axiom_allowlist=axioms)


#: Spec §4.1 names the generated bundle file `LeanAgent/Goals/Bundle_<digest>.lean`. That single
#: convention fixes both the module name a worker imports and, under `bundle_root`, the path its
#: compiled `.olean` sits at -- the two must agree, and `test_api.py` pins them together by
#: asserting the worker resolved the goal from exactly the path computed here.
_BUNDLE_NAMESPACE = ("LeanAgent", "Goals")


def _bundle_module_name(bundle_sha: str) -> str:
    """Deriving the module from the digest rather than accepting a module name keeps the sealed
    artifact content-addressed end to end: a caller can only ask to link against the bundle whose
    digest it names."""
    return ".".join((*_BUNDLE_NAMESPACE, f"Bundle_{bundle_sha}"))


def _bundle_olean_path(bundle_root: Path, bundle_sha: str) -> Path:
    return bundle_root.joinpath(*_BUNDLE_NAMESPACE, f"Bundle_{bundle_sha}.olean")


def _sha256_file(path: str) -> bytes | None:
    """`verdict.sealed_olean_sha_observed` -- the digest of the `.olean` the worker reports it
    actually resolved the sealed goal from, not one the caller supplied. `mark_proved` refuses to
    promote an obligation unless this equals `obligation.sealed_olean_sha` (see
    `deploy/grants.sql`), which is spec's seal-integrity check; recording an unverified value here
    would defeat it silently.

    Reads the path the *worker* named, which is correct only because workers are local
    subprocesses today. A future remote-worker deployment has to move this hashing into the worker
    itself -- the path would otherwise be meaningless on this side, or, worse, resolve to a
    different file with the same name.
    """
    try:
        with open(path, "rb") as handle:
            return hashlib.file_digest(handle, "sha256").digest()
    except OSError:
        return None


async def _run_link(
    pool: LeanReplPool,
    verdict_writer: VerdictWriter,
    base_env_sessionmaker: async_sessionmaker[AsyncSession],
    req: LinkRequest,
) -> LinkResponse:
    """Spec §6.2: "§4.2, then replay and audit; writes the `verdict` row"."""
    if req.paranoid:
        raise HTTPException(
            status_code=400,
            detail="paranoid (multi-kernel) replay is not available: this distribution ships one "
            "kernel implementation, and replaying twice through it would establish nothing",
        )
    base_env_digest = _parse_digest(req.base_env_digest)
    base_env = await _resolve_base_env(base_env_sessionmaker, base_env_digest)
    if base_env is None:
        raise HTTPException(
            status_code=404, detail=f"no base_env with digest {req.base_env_digest!r}"
        )
    obligation = await _resolve_obligation(base_env_sessionmaker, req.obligation_id)
    if obligation is None:
        raise HTTPException(status_code=404, detail=f"no obligation {req.obligation_id}")

    # Checked before a worker is acquired, not left to fail during `importModules`. A worker
    # spawned for a bundle that does not exist dies on startup, which `ReplWorker` correctly
    # classifies as a crash -- but `infra_error` means "retry may help", and no number of retries
    # will materialize a bundle nobody built. It also costs a pool slot and possibly an LRU
    # eviction to learn nothing. Confirmed empirically: without this the response was
    # `infra_error` with the diagnostic "stdout closed (process exited) while awaiting response".
    if pool.bundle_root is None:
        raise HTTPException(
            status_code=404,
            detail="this leanserv has no bundle_root configured, so no sealed bundle can be "
            "imported and nothing can be linked against",
        )
    if not _bundle_olean_path(pool.bundle_root, req.bundle_sha).exists():
        raise HTTPException(
            status_code=404,
            detail=f"bundle {req.bundle_sha} is not materialized under {pool.bundle_root} -- "
            "its .olean must be built out of band before it can be linked against",
        )
    bundle_module = _bundle_module_name(req.bundle_sha)
    # A distinct pool key from the plain `/v1/check` one for the same base env: this worker's
    # imports include the bundle, and `LeanReplPool.acquire` requires a key's imports to be stable
    # for its whole lifetime. That means one warm slot per (base env, bundle) pair, which is the
    # cost spec §6.2 already names for preludes ("a prelude-bearing worker occupies a full slot")
    # and gate 9 measured -- not an accident of this keying.
    pool_key = f"{req.base_env_digest}+{bundle_module}"
    imports = (*base_env.imports, bundle_module)

    started = time.monotonic()
    async with pool.worker(pool_key, imports) as worker:
        try:
            result = await worker.link(
                goal=req.goal,
                entry=req.entry,
                development=req.development,
                allow_axioms=obligation.axiom_allowlist,
                timeout_ms=req.timeout_ms,
            )
        except ReplTimeout as exc:
            # Spec §4.3 is explicit: "A replay timeout is not an acceptance: it yields
            # verdict_kind = 'timeout', replay_ok = false, and the obligation stays open."
            return await _write_link_verdict(
                verdict_writer,
                req,
                base_env,
                kind=VerdictKind.TIMEOUT,
                result=None,
                diagnostics=[str(exc)],
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except ReplCrashed as exc:
            # A verdict row is still written, and `attempt_id` is `verdict`'s primary key, so this
            # attempt can never receive another one. That is the intended shape: retrying means a
            # *new* attempt row, and `infra_error` is a first-class verdict kind precisely so a
            # crash is recorded as what it was rather than silently retried into a proof failure.
            return await _write_link_verdict(
                verdict_writer,
                req,
                base_env,
                kind=VerdictKind.INFRA_ERROR,
                result=None,
                diagnostics=[str(exc)],
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    kind = VerdictKind.PROVED if result.ok and result.axiom_audit_ok else VerdictKind.ERRORS
    return await _write_link_verdict(
        verdict_writer,
        req,
        base_env,
        kind=kind,
        result=result,
        diagnostics=list(result.diagnostics),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


async def _write_link_verdict(
    verdict_writer: VerdictWriter,
    req: LinkRequest,
    base_env: _ResolvedBaseEnv,
    *,
    kind: VerdictKind,
    result: LinkResult | None,
    diagnostics: list[str],
    elapsed_ms: int,
) -> LinkResponse:
    """Write the one `verdict` row this request produces, then answer with the same facts.

    `result is None` is the infra path (timeout or crash): every acceptance flag is false and no
    digest was observed, because nothing was actually judged. Spec's own principle -- an infra
    failure is not evidence about the content -- is why those flags must be false rather than
    absent or carried over from some earlier attempt.
    """
    observed = (
        _sha256_file(result.goal_olean_path)
        if result is not None and result.goal_olean_path is not None
        else None
    )
    await verdict_writer.write(
        VerdictInput(
            attempt_id=req.attempt_id,
            obligation_id=req.obligation_id,
            kind=kind,
            link_ok=result.link_ok if result else False,
            replay_ok=result.replay_ok if result else False,
            axiom_audit_ok=result.axiom_audit_ok if result else False,
            sealed_olean_sha_observed=observed,
            axioms=result.axioms if result else None,
            elapsed_ms=elapsed_ms,
            toolchain_rev=base_env.toolchain_rev,
            mathlib_rev=base_env.mathlib_rev,
            messages=json.dumps(diagnostics).encode() if diagnostics else None,
            # The development that produced this verdict. `verdict` is the one row leanserv writes
            # and the only place the accepted proof text can live without widening `app`'s grants:
            # `obligation.proof_blob` is not in app's permitted-column list, deliberately. Spec
            # §6.3 step 6 materializes the output file from exactly this.
            proof=req.development.encode(),
        )
    )
    return LinkResponse(
        kind=kind,
        link_ok=result.link_ok if result else False,
        replay_ok=result.replay_ok if result else False,
        axiom_audit_ok=result.axiom_audit_ok if result else False,
        axioms=list(result.axioms) if result else [],
        kernels_agreeing=[],
        elapsed_ms=elapsed_ms,
        cache_hit=False,
        diagnostics=diagnostics,
    )


async def _run_decompose(
    pool: LeanReplPool,
    base_env_sessionmaker: async_sessionmaker[AsyncSession],
    req: DecomposeRequest,
) -> DecomposeResponse:
    """Spec §6.2's `/v1/decompose`: "`sorry` extraction → `Decomposition`".

    Uncached, for the same reason `/v1/seal` is: `verification_cache` answers "did this declaration
    check", and a `CachedCheck` has nowhere to carry per-lemma statements or the reassembly source.

    Needs no bundle on the worker's path, unlike `/v1/link` -- decomposition reads a development's
    own `sorry`s and never touches a sealed goal, so it runs on the plain base-env worker `/v1/check`
    already uses, sharing its warm slots rather than fragmenting the pool further.
    """
    base_env_digest = _parse_digest(req.base_env_digest)
    base_env = await _resolve_base_env(base_env_sessionmaker, base_env_digest)
    if base_env is None:
        raise HTTPException(
            status_code=404, detail=f"no base_env with digest {req.base_env_digest!r}"
        )

    started = time.monotonic()
    async with pool.worker(req.base_env_digest, base_env.imports) as worker:
        try:
            result = await worker.decompose(req.development, timeout_ms=req.timeout_ms)
        except ReplCrashed as exc:
            # 503 rather than `ok=False`: `ok=False` here means "the development does not
            # elaborate", a statement about the content. A crashed worker judged nothing, and
            # spec's `infra_error` principle is that such a failure is not evidence about content.
            raise HTTPException(status_code=503, detail=f"lean worker unusable: {exc}") from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)

    return DecomposeResponse(
        ok=result.ok,
        lemmas=[
            DecomposedLemmaResponse(
                name=lemma.name,
                statement=lemma.statement,
                level_params=list(lemma.level_params),
                round_trips=lemma.round_trips,
                diagnostics=list(lemma.diagnostics),
            )
            for lemma in result.lemmas
        ],
        reassembly=result.reassembly,
        diagnostics=list(result.diagnostics),
        elapsed_ms=elapsed_ms,
    )


def create_app(
    pool: LeanReplPool,
    cache: VerificationCacheStore,
    verdict_writer: VerdictWriter,
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

    @app.post("/v1/seal", response_model=SealResponse)
    async def seal(req: SealRequest) -> SealResponse:
        return await _run_seal(pool, base_env_sessionmaker, req)

    @app.post("/v1/decompose", response_model=DecomposeResponse)
    async def decompose(req: DecomposeRequest) -> DecomposeResponse:
        return await _run_decompose(pool, base_env_sessionmaker, req)

    @app.post("/v1/link", response_model=LinkResponse)
    async def link(req: LinkRequest) -> LinkResponse:
        return await _run_link(pool, verdict_writer, base_env_sessionmaker, req)

    @app.get("/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        pool_health = await pool.health()
        return HealthResponse(**dataclasses.asdict(pool_health))

    return app
