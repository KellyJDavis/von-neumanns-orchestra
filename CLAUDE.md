# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

Phase 0 (foundations) is complete and committed: `uv` workspace with stub packages, the `leankernel` Lake package
pinned to Mathlib v4.33.1, GitHub Actions CI, `deploy/Dockerfile.base`, a `deploy/grants.sql` role skeleton, and
`docs/provenance.md`. Phase 1 (the acceptance path) is in progress — M1.1 (Seal + Audit), M1.2 (Link), M1.3
(Replay), M1.4 (Sorries/Infotree decomposition), M1.5 (DDL, Alembic, Pydantic mirror), M1.6 (privilege model +
`mark_proved`), and M1.7 (content-addressed blob store) are implemented and tested. M1.8 (`leanserv`) is complete
and, having been far larger than M1.1–M1.7, was split into its own sub-milestones rather than one PR: M1.8.1
(`leankernel serve`), M1.8.2 (Python `repl.py` process wrapper), M1.8.3 (`pool.py` LRU), M1.8.4 (`cache.py` L0/L1 +
`verdicts.py`), M1.8.5 (`api.py` FastAPI surface: `/v1/check`, `/v1/check_batch`, `/v1/health` — `/v1/seal`,
`/v1/link`, `/v1/replay`, `/v1/decompose`, and `/v1/base-env/materialize` are deliberately not built yet; see
`api.py`'s own module docstring and the implementation notes below for why). M1.9 (eval harness skeleton --
`packages/eval`: `score.py`, `reverify.py`, `contamination.py`, the internal regression suite) is also complete.
Gate 7 (decomposition round-trip on a Mathlib sample) is also done — `LeanKernel/DecomposeFuzz.lean`, exercised
both on every commit at small scale (`kernel_tests`) and, periodically/manually, at the gate's own named scale via
`lake exe leankernel decompose-fuzz <seed> <count>`. Phase 1's remaining scope is gate 1's 10k-proof throughput
report (blocked on the Lean Workbook corpus targeting the wrong toolchain version — unresolved since Phase 1
planning) and gate 9's prelude memory delta, not new modules. See "Implementation notes" below for load-bearing
facts discovered while building all of the above. Read
[von-neumanns-orchestra-spec-v1.md](von-neumanns-orchestra-spec-v1.md) in full before implementing anything
further — it isn't a proposal, it's what to build from. Section numbers below (§N) refer to sections of that
file. Follow the layout and stack it fixes rather than improvising a different structure; if you deviate, update
the spec, don't let them drift apart.

## What this system is

An agentic Lean 4 theorem-proving system: it takes a Lean file containing `sorry`s (or a bare statement),
decomposes it into independently schedulable proof obligations, proves them with a mix of symbolic tactics and
LLMs, and returns a file whose theorems are verified to prove exactly what was asked — with functional parity to
`project-numina/numina-lean-agent`.

Two decisions drive nearly everything else in the design (spec §1):

- **The core invariant**: an obligation is `proved` only when it *links* (kernel accepts the agent's term at the
  sealed goal's type), *replays* (re-checks in a fresh kernel via `lean4checker`), *audits* (axioms within the
  run's allowlist), and *seal integrity* holds (digest match). The agent **never writes the goal statement** —
  it's elaborated once, before the agent runs, in an environment the agent cannot influence, and frozen as a
  compiled constant. This makes statement drift structurally impossible rather than something to detect.
- **The core scheduling decision**: the unit of work is the *proof obligation*, not the file. Obligations form a
  DAG with competing decomposition groups, which is what makes caching, resumption, budget allocation,
  backtracking, and multi-user fairness expressible.

## Planned tech stack (spec §2)

- **Orchestration**: Python 3.12, `uv` workspace, `asyncio` + `uvloop`
- **Control loop**: hand-written Postgres state machine (deliberately not Temporal/LangGraph/Celery — state is
  already externalized in Postgres and the topology isn't static)
- **Persistence**: PostgreSQL 16, SQLAlchemy 2.0 async + Alembic, Pydantic v2 for every schema
- **HTTP**: FastAPI + uvicorn (server), httpx (client)
- **Lean toolchain**: pinned per run, 4.23 minimum / 4.29+ recommended; `leanprover-community/repl` pooled, a fork
  of Kimina Lean Server
- **Lean-side code**: a Lake package on Mathlib using `Lean.Elab` metaprograms — never regex/tree-sitter over
  source
- **Models**: vLLM primary, OpenAI-compatible `/v1/completions` with **token ids** (never
  `/v1/chat/completions` — that loses token identity needed for replay and RL), LiteLLM isolated to one module
  for closed APIs
- **Sandbox**: bubblewrap → gVisor → Firecracker, escalating with multi-tenancy
- **Lint/types/test**: `ruff`, `mypy --strict`, `pytest`, `hypothesis`

## Commands

- Python: `uv sync --all-packages`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy --strict packages`, `uv run pytest` (all run from the repo root; mirrors `.github/workflows/ci.yml`).
- Lean (`packages/leankernel`): `lake build` (builds `LeanKernel` + the `leankernel` exe), `lake test` (builds
  and runs `kernel_tests`, the Audit/Seal test suite — this is what CI's `lean` job runs via `lean-action`'s
  `test: true`). Run `lake exe kernel_tests` directly for a single test run without going through `lake test`'s
  dependency check.
- `leankernel serve` (M1.8.1, the persistent warm-environment process `leanserv`'s pool will spawn one of per
  worker): `lake exe leankernel serve [<import>...]` (e.g. `serve Init` for a fast Mathlib-free session, or
  `serve Mathlib.Algebra.Group.Defs` for a real Mathlib base env), then write newline-delimited JSON requests to
  its stdin — e.g. `printf '{"id":"1","body":"def foo : Nat := 5"}\n' | lake exe leankernel serve Init` — and
  read one JSON response line per request from stdout. Closing stdin exits it with code 0.
- `leankernel decompose-fuzz` (gate 7's periodic/manual validation run): `lake exe leankernel decompose-fuzz
  <seed> <count> [<import>...]` (imports default to `Mathlib` if none given) — samples `<count>` real theorems
  from the imported modules and round-trip-checks each (spec's gate 7: children link standalone, reassembly
  links against the parent, no new axioms), printing a JSON `{sampleSize, passed, failures}` report and exiting
  nonzero if `failures` is non-empty. At the gate's own named scale (5,000-10,000), budget 20-35 minutes
  (measured: ~213ms/declaration). A small (30-declaration), fixed-seed slice runs on every `lake test` instead,
  as `kernel_tests`' own `decomposeFuzzChecks`.
- `leanserv`'s `ReplWorker` and `LeanReplPool` (M1.8.2/M1.8.3, `packages/leanserv/src/lean_agent_serv/{repl,pool}.py`):
  `lake build` in `packages/leankernel` first (both spawn the real `leankernel serve` binary from that build,
  never a mock), then `uv run pytest tests/leanserv`. The suite skips gracefully if that binary isn't built yet,
  the same way `tests/db/` skips if Postgres isn't reachable — CI's `lean` job builds it first specifically so
  the skip never triggers there (see `.github/workflows/ci.yml`).
- Database (`packages/core`'s `lean_agent_core.orm`/`.schemas`, `migrations/`): start Postgres 16 with
  `docker compose -f deploy/compose.yaml up -d`, then apply migrations with
  `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/leanagent uv run alembic upgrade head`,
  then run the schema round-trip tests with
  `TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/leanagent uv run pytest tests/db`.
  `tests/db/` runs against a real PostgreSQL instance, never a mock (spec §3) — its own connectivity check skips
  gracefully (not a fake pass) if Postgres isn't reachable locally, but CI always provides one via a service
  container specifically so the skip never triggers there. `DATABASE_URL` drives Alembic (async, matching spec's
  SQLAlchemy 2.0 async choice); `TEST_DATABASE_URL` drives the test suite's own sync engine — they're separate
  env vars because a schema-verification test has no reason to need an async driver.
- After changing `orm.py`: regenerate with `alembic revision --autogenerate -m "..."` against a running Postgres,
  then verify it with `alembic check` (should report no drift) and by actually applying it — autogenerate's
  output needs verification, not blind trust (see the enum-type finding below).
- Privilege model (`deploy/grants.sql`, spec §5.5): after migrations, apply it with
  `psql postgresql://postgres:postgres@localhost:5432/leanagent -f deploy/grants.sql` — note the plain
  `postgresql://` URL, not `DATABASE_URL`'s `postgresql+asyncpg://` (psql/libpq don't understand the SQLAlchemy
  driver suffix). It's idempotent (safe to re-run). CI applies it via `psycopg` instead of the `psql` binary, so
  it doesn't depend on a Postgres client being preinstalled on the runner — see `.github/workflows/ci.yml`. Then
  `tests/db/test_privileges.py` exercises it (gate 8) the same way `test_schema.py` exercises the schema.
- `leanserv`'s `VerificationCacheStore` and `VerdictWriter` (M1.8.4, `packages/leanserv/src/lean_agent_serv/
  {cache,verdicts}.py`): both need a live Postgres with migrations *and* `deploy/grants.sql` applied (they
  connect as the real `leanserv` role), so their tests live under `tests/db/` (`test_cache.py`/`test_verdicts.py`),
  not `tests/leanserv/` — no Lean process involved at all. `admin_engine`/`app_database_url`/
  `leanserv_database_url`/`leanserv_async_database_url`/`sealed_obligation` are shared fixtures in
  `tests/conftest.py` (top-level, an ancestor of every `tests/*` directory — see M1.8.5's note below on why it
  isn't `tests/db/conftest.py` anymore); run with `uv run pytest tests/db`.
- `leanserv`'s FastAPI surface (M1.8.5, `packages/leanserv/src/lean_agent_serv/api.py`): needs *both* the built
  `leankernel` exe and a live Postgres with `deploy/grants.sql` applied at once, so its tests
  (`tests/leanserv/test_api.py`) live alongside `test_repl.py`/`test_pool.py` rather than under `tests/db/`. CI's
  `lean` job grew a Postgres service container specifically for this (see `.github/workflows/ci.yml`); locally,
  `lake build` in `packages/leankernel`, then a reachable Postgres with migrations + grants applied, then
  `uv run pytest tests/leanserv`.
- `packages/eval` (M1.9, eval harness skeleton): `score.py`/`contamination.py` are pure logic (`uv run pytest
  tests/eval/test_score.py tests/eval/test_contamination.py`, no external infra); `reverify.py` and the internal
  regression suite (`suites/internal.py`) spawn real Lean processes, same `lake build` prerequisite and skip
  convention as `tests/leanserv/` — run all of it with `uv run pytest tests/eval`.

## Repository layout (spec §3, once scaffolded)

```
packages/
├── core/            lean_agent_core — schemas, protocols, obligation state machine, scheduler, worker (control
│                    loop), budget, ContextBuilder, blob store
├── leankernel/      Lean 4 Lake package: Audit, Seal, Link, Replay, Sorries, Infotree, Main
├── leanserv/        REPL pool + LRU, cache tiers L0-L3, verdict writer (only writer of verdict rows), FastAPI
├── models/          typed client, client-side chat templating to token ids, ModelRole router, LiteLLM adapter
├── policies/        SymbolicPortfolio, WholeProofSampler, RepairLoop, DecomposeAndConquer, InformalFirst,
│                    BestFirstDAG (post-MVP), versioned prompts
├── tools/           search, informal, transform, references (MCP wrappers are post-MVP, same code)
├── api/             public FastAPI app, routes, ingestion (file → sealed goals → obligations)
├── eval/            suites (miniF2F, Putnam, sorrydb-style), scoring, clean-container reverification
└── cli/             run, batch, from-folder, status, materialize
plugins/             anything that cannot be open-sourced — excluded from default install and all benchmark
                     configs, enforced in CI; packages/* must never import from plugins/
migrations/          Alembic
deploy/              Dockerfiles, compose.yaml, grants.sql (privilege model, tested not assumed)
```

Every `packages/*` entry is independently publishable under Apache-2.0 with DCO.

**Deviation from the spec's literal layout**: `leankernel`'s own unit tests (`AuditFixtures.lean`, `Goals.lean`,
`Main.lean` driving the `kernel_tests` executable) live under `packages/leankernel/LeanKernelTests/`, not the
top-level `tests/kernel/` the spec's tree sketch implies. Reason: `tests/kernel/` as a separate Lake package would
need its own `require mathlib`, duplicating a second multi-GB fetch/build of a dependency `packages/leankernel`
already resolves — co-locating tests inside the package that owns them avoids that for zero real cost. Reserve
top-level `tests/kernel/` for adversarial developments that genuinely need to sit outside any single package (e.g.
gate 7's stratified Mathlib sample, at the 5,000–10,000-declaration scale, as a periodic/manual exit-gate run
rather than something `lake test` runs on every commit).

Most of `kernel_tests` is deliberately Mathlib-free for speed, but M1.4's decomposition tests are the first
exception: verifying the anonymous-instance-binder handling in `abstractSorry`/`decompose` genuinely needs a real
Mathlib goal (`import Mathlib.Algebra.Group.Defs`; spec's own Appendix C names this as only surfacing on
real Mathlib-dependent goals, and it did — see below). This is a deliberate, small, targeted exception (one
lemma, one import), not a change to the general Mathlib-free policy for the rest of the suite.

`Goals.lean` specifically must stay a *genuinely compiled* module (part of the normal `LeanKernelTests` lean_lib
build), not something defined inline in a test's dynamically-elaborated source string — see the Link-testing note
below for why that distinction is load-bearing, not incidental.

## Architecture notes that require reading multiple sections to piece together

**The acceptance path is one pipeline, not four independent checks** (spec §4). Seal happens once at obligation
creation in a warm REPL (never cold through `lake` — cold per-child compilation measured at ~78% of pipeline
time vs. <1% warm). Link resolves the agent's declaration by name against the sealed goal and adds it to the
kernel with forced options (`debug.skipKernelTC` explicitly false), bypassing whatever `Lean.addDecl` would
otherwise respect. Replay (`lean4checker`) re-derives the environment from imports to catch environment hacking
that link alone wouldn't — but replay's trust base excludes Mathlib itself (`.olean` loading does no kernel
checking), so that trust rests entirely on read-only mounts and image digests, not on replay. Audit uses an
**allowlist, never a deny-list**, and property tests rather than axiom-name lists, because Lean 4.29 silently
changed how `native_decide` axioms are named — a name-list would have gone quietly blind.

**Decomposition children are unconditionally trusted; only root obligations get admission checks** (spec §4.5,
§4.6). A subgoal that `simp` closes trivially is fine — admission signals (near-instant symbolic closure, a
found counterexample, `autoImplicit` silently firing, free universe metavariables) only matter on submitted
roots, since only there do they indicate mis-formalization rather than an easy step. Reassembly (recombining
proved children into the parent) is itself a full link/replay/audit, not bookkeeping — a failed reassembly fails
only that decomposition group, and proved children remain reusable by a competing group (this is what
`group_id` on `obligation_edge` is for).

**The obligation state machine and the privilege model are two views of the same guarantee** (spec §5.5, §6.4).
`mark_proved` is a `SECURITY DEFINER` Postgres function and the *only* path to `proved` status — application
code gets column-level `UPDATE` on `obligation` (never table-level), and only `leanserv` can `INSERT` into
`verdict`. Workers request a check and observe the outcome; they never transcribe it. A table-level `GRANT
UPDATE` on Postgres confers update on every column and a later column-level `REVOKE` does **not** subtract from
it — grants must be written grant-only, not grant-then-revoke.

**`infra_error` is a first-class, unbudgeted outcome, not a proof failure** — conflating worker OOM/crash with a
genuine proof failure is called out as the most common way these systems silently report a wrong pass rate.
Anywhere you're tempted to fold error handling into the failure path, check whether it's actually infra.

**Policies never touch models or the DB directly.** A `Policy.propose()` yields `Action`s
(`SubmitProof | Decompose | CallTool | RequestCompletion | Abandon`); the *executor* performs the side effects.
This is what keeps trajectories replayable and the tool allowlist enforceable, and why `trajectory.provenance`
can be derived from `ModelBackend.provenance` at registration rather than asserted by whatever called it.
Provenance tracking (open_weights / symbolic / human / closed_api_eval_only) exists because the binding
constraint on open-weights output is training-data provenance, not code licensing — the corpus exporter raises
(does not filter) on anything outside `{open_weights, symbolic, human}`.

**Context bands are ordered by eviction priority, and two rules matter more than the bands themselves** (spec
§6.6): never summarize kernel output (truncate with an explicit elision marker instead, so the model can request
more rather than hallucinate), and keep bands 1–2 (sealed goal, kernel diagnostics) byte-stable across resamples
or prefix caching silently stops working.

**Header fragmentation, not compute, is the binding multi-tenancy constraint** (spec §6.2, §9.2). Each warm REPL
worker is keyed by `base_env_digest`, and a 256 GiB node supports only ~5 concurrently warm base environments at
a 12 GiB/worker memory cap — arbitrary user import lists fragment this catastrophically. Project-local libraries
are handled as *preludes* within a base environment (avoids re-elaboration cost) but still cost a full memory
slot; the post-MVP fix is a copy-on-write Mathlib "zygote" fork, not more RAM.

**R18 (spec §10) is the risk that should shape any RL/training work**: an optimizing prover under reward
pressure attacks the kernel implementation itself, not the theorem — this has already happened once via a
reference-counting/GMP bug. No single mitigation is sufficient; kernel disagreement is treated as a security
incident with the trajectory preserved (never retried as flaky), and RL reward should key on multi-kernel
agreement, not single-kernel acceptance.

## Implementation notes: Lean-runtime facts that will recur

These surfaced while building M1.1 (`packages/leankernel/LeanKernel/{Audit,Seal}.lean`), M1.2 (`Link.lean`), M1.3
(`Replay.lean`), M1.4 (`Infotree.lean`, `Sorries.lean`), M1.8.1 (`Serve.lean`), and gate 7 (`DecomposeFuzz.lean`)
by testing directly against the real v4.33.1 toolchain rather than assuming from memory. They are load-bearing
for `leanserv`, which needs this exact capability — elaborating arbitrary fresh source from a compiled process —
in production, not just in tests.

- **`lean4checker` is deprecated**: merged into Lean itself as `leanchecker`, built into every toolchain since
  v4.28.0 (`lake env leanchecker`, or `--fresh` to replay into a fresh environment). Don't add it as a Lake
  dependency; the spec predates this merge. `Replay.lean` doesn't shell out to either tool — it calls the public
  primitive both are built on directly: `Lean.Environment.replay (newConstants : Std.HashMap Name ConstantInfo)
  (env : Environment) : IO Environment`, which type-checks each given constant against a base environment via the
  kernel, independent of however those constants originally got elaborated.
- **A `lean_exe` that elaborates fresh source at runtime (not just pre-compiled modules) needs
  `supportInterpreter := true`** in its lakefile target. Without it, parsing works (notation loads fine from
  imported `.olean` data) but every builtin term elaborator silently reports "has not been implemented" —
  builtin elaborator entries are native closures, not serializable data, and populating them requires the
  interpreter. `leanprover-community/repl` sets this flag; it is easy to omit and the failure mode doesn't
  obviously point at the cause.
- **The correct pattern for elaborating a fresh source string** (confirmed against `leanprover-community/repl`'s
  own `processInput`): call `enableInitializersExecution`, then `Parser.parseHeader` on the input to get its own
  `import` line, then `Lean.Elab.processHeader`, then `Lean.Elab.IO.processCommands`. Do **not** use
  `withImportModules` for this — it hardcodes `loadExts := false`, which is fine for inspecting already-compiled
  constants (that's what `Audit.lean`'s tests use it for) but leaves notation/elaborator extensions unpopulated
  for fresh elaboration. Also never return a raw `Environment` out of `withImportModules`'s callback — it frees
  the environment's compacted regions the moment the callback returns, so anything held past that point segfaults
  on use; do all such work *inside* the callback.
- **Lean's error recovery is deceptive for seal-style validation**: a command that fails to elaborate (e.g. an
  unknown identifier) still leaves a `#check`-able constant in the environment, backed by `sorryAx`. Existence in
  the environment is *not* evidence that elaboration succeeded — check the message log's errors, and, as
  `Seal.lean` does, also verify the declaration's axiom cone is empty (a `sorry` in the statement itself
  elaborates with only a *warning*, not an error, so the error-log check alone would miss it).
- **Top-level `def` universe generalization is unconditional**: Lean auto-generalizes a free universe
  metavariable into an explicit level parameter regardless of `autoImplicit`/`relaxedAutoImplicit` (confirmed:
  `set_option autoImplicit false` does not change this). Those options are still forced per spec §4.1, but don't
  expect them to be what causes "a remaining universe metavariable is an admission failure" — in practice that
  case manifests as an ordinary elaboration error, already covered by the message-log check above.
- **`getModuleIdxFor?` only ever returns `some` for a constant that came from a *different, already-compiled*
  module** — "async constants are always from the current module" (the doc comment's own words), meaning anything
  defined inline in the module currently being elaborated always returns `none`. This is why `Link.lean`'s test
  fixtures (`LeanKernelTests/Goals.lean`) must be a genuinely separate, normally-compiled module rather than
  something defined inline in the dynamically-elaborated test source — an inline "goal" can never satisfy Link's
  shadow-detection check, since it would never be "imported" in the first place. In production this means Link's
  shadow check is only meaningful once a sealed goal bundle has actually been compiled and imported for real, not
  while it's still an in-session-only declaration in the warm REPL that sealed it.
- **Lean itself refuses to redeclare an already-declared name** ("has already been declared", confirmed
  empirically), even across separate commands processed against the same accumulating environment. So Link's
  `expectedModuleIdx` check is not primarily defending against an agent redeclaring the goal mid-session — Lean's
  own elaborator already blocks that outright, before Link would ever run. The check's real value is against
  *infrastructure* resolving the goal's name to the wrong module in the first place (a stale cached bundle, a
  search-path ordering bug) — worth knowing so the check isn't miscast as an adversarial-agent mitigation when
  writing future tests or docs around it.
- **A "data-producing" (non-`Prop`) sealed goal's *value* is the type to inhabit, not another type-former to
  restate.** For `def G_poly.{u} : Sort u := PUnit.{u}`, a valid entry is `def sol.{v} : PUnit.{v} := PUnit.unit`
  — something whose own type unfolds to `G_poly`'s value — *not* another `Sort v := PUnit.{v}`-shaped definition
  mirroring the goal's own shape. Got this wrong on the first pass (mirrored the goal instead of inhabiting it),
  and the kernel's "declaration type mismatch" error names the mismatch precisely if it happens again.
- **Link and Replay check genuinely different things and neither is a substitute for the other.** Link verifies
  *entry matches goal* (kernel type-checks one constructed declaration, trusting whatever types are already
  stored for `entry` and its dependencies). Replay verifies *everything newly introduced is kernel-sound on its
  own terms*, independent of whatever ambient options were active when it was elaborated — confirmed by running
  both on the exact same poisoned-`debug.skipKernelTC` session from M1.2's gate 4 test: Link still correctly
  rejects the entry-vs-goal mismatch, and Replay *separately* confirms the agent's own declaration is internally
  well-typed, unaffected by the poisoning either way, because `Environment.replay` never reads the session's
  options at all — it calls `addDeclCore` with hardcoded values. Don't expect Replay to "extra-catch" a Link
  rejection; a properly-formed weakened-mutant test has nothing wrong with it *except* not matching the goal, and
  Replay isn't checking that relationship.
- **A raw `Kernel.Environment` cannot be constructed from outside `Lean`** (its constructor is `private`), which
  is why `Replay.lean`'s test for "does replay actually reject bad data" calls the lower-level
  `Lean.Environment.replay` primitive directly with a hand-built, deliberately mistyped `ConstantInfo`, rather
  than trying to route a bogus constant through `LeanKernel.replay`'s own environment-diffing wrapper. There is no
  public API for smuggling an unchecked constant into an `Environment` in the first place — good to know before
  assuming a "realistic environment-hacking" test needs to look more elaborate than this.
- **`abstractSorry` reuses `MVarId.revert` rather than hand-rolling binder abstraction** — the same mechanism the
  `revert` tactic itself uses, via `collectForwardDeps` for dependency-correct ordering and `BinderInfo`
  preservation for instance-implicits. This is a reuse-over-reinvent choice, same spirit as `Audit.lean` reusing
  `collectAxioms`: spec §4.6 calls out binder ordering and instance-implicit re-binding as needing special
  handling, and `revert` already handles both correctly because dependency-correct reverting is its entire job.
- **`revert`'s own return value — the fvars it actually reverted — must be used for the reassembly application,
  not `LocalContext.getFVarIds` recomputed independently.** A top-level `theorem` proved by tactic carries an
  auxiliary "recursive reference to self" declaration in its raw local context (so the proof body could refer to
  itself for well-founded recursion); `revert`'s `clearAuxDeclsInsteadOfRevert` correctly drops it, but
  recomputing the argument list from `g.lctx.getFVarIds` afterward silently reintroduces it as a bogus extra
  argument — confirmed empirically (`exact (sorry_1 parent1)`, applying the lemma to the theorem itself), and it
  surfaced on the simplest possible synthetic test, not only on real Mathlib goals. `Sorries.lean`'s `decompose`
  derives argument text from the reassembly term's own application spine specifically to make this impossible to
  get wrong twice.
- **`abstractSorry`'s returned type can carry a newly-generalized universe parameter that `Decomposition.lemmas`
  has nowhere to record** (spec's own `Array (Name × Expr)` has no `levelParams` slot). Declaring the lemma with
  `levelParams := []` produces "invalid reference to undefined universe level parameter" the moment the goal has
  a `Type*` binder — confirmed on the real Mathlib instance-implicit test below. Any real caller of
  `Decomposition.lemmas` (this project's own round-trip test included) must derive `levelParams` itself via
  `Lean.collectLevelParams {} ty |>.params`, not assume `[]`.
- **An anonymous instance binder (Mathlib's default style, e.g. `[Group G]`) gets a hygiene-mangled name with no
  valid surface syntax at all** — not just an inaccessible-but-nameable identifier; splicing it verbatim is a
  parse error, confirmed empirically. `decompose` detects this via `Name.hasMacroScopes` and splices `‹Type›`
  (anonymous instance-lookup-by-type syntax) instead of the name — which must be pretty-printed with the sorry's
  own `LocalContext` actually ambient (`withLCtx`), or a type like `Group G` prints its own fvar `G` as a raw
  internal id instead of its display name.
- **Reapplying captured arguments in reassembly must use `@`-prefixed (fully explicit) application, never plain
  application.** A goal's implicit (`{G}`) and instance-implicit (`[Group G]`) parameters are exactly the ones
  ordinary application auto-fills with fresh metavariables before consuming explicit arguments — so passing them
  positionally without `@` shifts every subsequent argument into the wrong slot (confirmed empirically: `sorry_1
  G ‹Group G› a b` elaborated as if `G` were the first *explicit* argument). This also matters for
  reproducibility, not just correctness: `@`-application reproduces the *exact* captured context deterministically
  rather than relying on typeclass search or unification to re-derive it, which is the more fragile alternative
  and was rejected for that reason, independent of whether it happened to also work.
- **`Lean.Elab.process (input) (env) (opts) : IO (Environment × MessageLog)` elaborates `input` against an
  already-imported `env` and returns a *new* environment, leaving `env` itself untouched** — confirmed by calling
  it twice in a row against the same `baseEnv` with unrelated declarations and observing the second call's
  `MessageLog` report an unknown-identifier error for the first call's declaration. This is exactly the isolation
  `serve` needs (unrelated agent attempts checked against the same warm worker must never see each other's
  declarations) and requires no scoping helper of its own — unlike `Link.lean`'s `withEnv`, there is nothing to
  restore, since the base environment was never mutated in the first place.
- **`importModules`, not `withImportModules`, is the correct call for an environment meant to outlive the call
  that built it.** `withImportModules`'s own docstring says as much (frees compacted regions when its callback
  returns), and M1.1 already paid for getting this wrong once (see below) — worth restating here because `serve`
  is the first place the *particular* failure mode (a base environment meant to survive for a whole process
  lifetime, not just one callback) actually arises in production code rather than a test.
- **`IO.FS.Handle.getLine` returns `""` only at genuine EOF, never for an actual empty input line** (which still
  carries the trailing newline `getLine` strips) — confirmed against the primitive's own docstring, not assumed.
  This is what makes a bare `while` loop reading `stdin.getLine` until empty a correct EOF-driven server loop
  rather than one that would misfire on a blank request line.
- **Output must be flushed after every response line, not just written.** `IO.FS.Stream.putStrLn` only appends to
  a buffer; a caller on the other end of a pipe blocking on its own `readline()` would hang indefinitely on a
  response `serve` has already computed but not yet handed to the OS. `runServe` calls `stdout.flush` after every
  response for exactly this reason — confirmed necessary, not defensive-only, by testing over a real OS pipe
  (`printf ... | lake exe leankernel serve ...`), not just by reasoning about buffering in the abstract.
- **`String.trim` is deprecated in v4.33.1 in favor of `String.trimAscii`, which returns a `String.Slice`, not a
  `String`** — the compiler catches this immediately (a genuine type mismatch, not merely a style lint), and the
  fix is `.trimAscii.toString`, not switching back to the deprecated function.
- **A dotted CLI module name (e.g. `Mathlib.Algebra.Group.Defs`) is safest built by hand
  (`(s.splitOn ".").foldl Name.mkStr Name.anonymous`) rather than via an assumed `String → Name` stdlib helper.**
  A `Slice.toName` exists but its own docstring example (`"a.b".toSlice.toName` naming `a.b`, distinct from the
  escaped `«a.b»`) wasn't confirmed to compose the same way for a multi-component dotted path without directly
  reading its implementation; the fold is three lines, has no ambiguity to check, and is exactly what a module
  path already means structurally.
- **`abstractSorry` had a real bug, found only by running it against real Mathlib content instead of synthetic
  test goals: its returned `reassemblyTerm` could reference the child with too few universe-level arguments.**
  `abstractSorry` builds the reassembly call's level arguments from `result.newParamNames` — the level params
  *its own* `levelMVarToParam` generalization step introduced from metavariables. That is not the same thing as
  "every level parameter the abstracted type actually depends on" whenever the original goal already mentions a
  *concrete* `Level.param` directly, with no metavariable for this step to generalize in the first place — exactly
  what happens when the "goal" is an already-compiled declaration's own type (gate 7's decomposition-fuzz
  harness), unlike M1.4's own synthetic test goals, which all introduced their universe polymorphism through a
  fresh implicit binder that elaborates as a metavariable first. The mismatch surfaced as a kernel rejection
  ("incorrect number of universe levels parameters") the moment such a child was actually declared and applied —
  it would never have shown up testing only hand-written goals with metavariable-introduced universes, which is
  exactly what M1.4's own suite did. **Fix**: derive `levelParams` (and therefore the reassembly term's level
  arguments) from `Lean.collectLevelParams` on the abstracted type itself, the same way `Sorries.lean`'s own
  `decompose` already told *callers* of `Decomposition.lemmas` they must (see the M1.4 note above) — the function
  now derives its own internal level list the same way, so the two can never disagree again.
- **`Environment.constants.fold` over a fully-loaded `Mathlib` import visits ~440,000 constants, in well under a
  second** — most of them compiler-generated (equation lemmas, match auxiliaries, projections), not things a
  person wrote. `Name.hasMacroScopes` alone does not filter these out (they're ordinary non-hygienic top-level
  names); gate 7's own eligibility filter additionally requires `ConstantInfo.thmInfo` (excludes `def`s,
  `instance`s, structure/projection auxiliaries) and a source module literally named `Mathlib...` (via
  `getModuleIdxFor?`), which was enough in practice to draw genuine, person-written theorems at every sample size
  tested (30 to 2,000).
- **Per-declaration cost for gate 7's round-trip check is ~213ms, dominated by `collectAxioms`'s transitive
  dependency walk (called twice per declaration), not by the kernel `addDecl` calls or by sampling itself.**
  Measured directly: importing all of `Mathlib` took ~2.7s, computing and sampling from the ~440,000-entry
  eligible pool took under 1ms combined (even the naive `O(count × pool.size)` `eraseIdx!`-based sampling — not
  worth the added complexity of a swap-and-pop `O(1)` removal unless a future run at far larger scale actually
  shows sampling itself as the bottleneck, which 2,000 real samples did not), and the remaining ~427s for 2,000
  declarations was essentially all in `decomposeFuzzOne` itself. This is why the gate's own named scale
  (5,000-10,000) is treated as a periodic/manual run (~20-35 minutes) rather than something the fast per-commit
  loop pays for — not a cost worth trying to engineer away for a check that only needs to run occasionally.

## Implementation notes: database facts that will recur

These surfaced while building M1.5 (`packages/core/src/lean_agent_core/{orm,schemas,enums}.py`, `migrations/`) by
applying migrations against a real Postgres 16 instance and round-tripping every table, rather than trusting
`alembic revision --autogenerate`'s output or the ORM model definitions on inspection alone.

- **Alembic's autogenerated `downgrade()` does not emit `DROP TYPE` for Postgres native enums.** Confirmed by
  actually downgrading then re-upgrading (not just upgrading once): `attempt_status`, `obligation_status`,
  `verdict_kind`, `trust_class`, and `provenance_class` all survived `downgrade()`'s table drops as orphaned
  types, and the following `upgrade()` failed with "type already exists" on the first one it tried to recreate.
  The initial migration's `downgrade()` now explicitly drops all five at the end, after every table referencing
  them is gone. Check for this on every future migration that adds or removes a `sa.Enum`-backed column — it
  will not show up from inspecting `upgrade()` alone, or from a single upgrade with no downgrade attempted.
- **A bare `Mapped[datetime]` column maps to Postgres `TIMESTAMP` (no timezone), silently dropping spec's
  `timestamptz` requirement**, unless told otherwise. Rather than annotating every one of the ~15 datetime
  columns individually, `orm.py`'s `Base` sets `type_annotation_map = {datetime: DateTime(timezone=True)}` once;
  confirmed via `\d` on the live table that every timestamp column came out `timestamp with time zone`.
- **`Index(..., "col")` sorts ascending by default, and spec is specific about which indexes need `DESC`**
  (`run(tenant_id, created_at DESC)`, `obligation_schedulable(..., priority DESC, ...)`,
  `attempt(obligation_id, started_at DESC)`). A first pass without `.desc()` on those columns created all three
  ascending; caught by inspecting the actual live index definitions via `\d`, not by reading the ORM code back
  against the spec text (which looked plausible either way at a glance).
- **`values_callable` is required to get lowercase enum values into Postgres.** `_pg_enum` passes
  `values_callable=lambda e: [member.value for member in e]` explicitly; without it, SQLAlchemy's `Enum` type
  uses each Python enum member's uppercase `.name` (`OPEN`, not `open`) for the underlying Postgres type's
  values, which would silently diverge from spec §5.2's lowercase enum values.
- **A test that connects as multiple roles cannot reuse the "wrap the test in a rolled-back transaction" isolation
  pattern** `test_schema.py` uses. `test_privileges.py` needs data inserted by an admin connection to be visible
  to entirely separate `app`/`leanserv` connections, and other transactions can never see another transaction's
  *uncommitted* work (ordinary MVCC) — confirmed empirically, the first version of that fixture used the
  roll-back pattern and every role-scoped test failed with a foreign-key violation because the referenced row
  was never actually committed. Its fixture genuinely commits and cleans up explicitly (`DELETE ... CASCADE`)
  instead.
- **Postgres roles created via `CREATE ROLE ... LOGIN` with no password cannot authenticate over TCP at all** —
  fine for `grants.sql` itself (production manages credentials separately), but `test_privileges.py` needs to
  actually connect as `app`/`leanserv` to exercise their grants, so its `admin_engine` fixture sets a test-only
  password on each via `ALTER ROLE ... PASSWORD ...` before any role-scoped test runs.
- **`psql`'s `-f` and psycopg's `.execute()` both run a whole multi-statement SQL file as one call** (libpq's
  simple query protocol, not the extended/parameterized one) — confirmed by applying `grants.sql`, which
  contains a `DO $$ ... $$` block and a multi-line `CREATE FUNCTION ... AS $$ ... $$` body, via
  `psycopg.Connection.execute(open("deploy/grants.sql").read())` with `autocommit=True`. CI uses this instead of
  shelling out to `psql`, specifically so applying grants doesn't depend on a Postgres client being preinstalled
  on the runner.

## Implementation notes: blob store facts

These surfaced while building M1.7 (`packages/core/src/lean_agent_core/{blobs,protocols}.py`), a pure-filesystem
content-addressed store with no Postgres dependency — tested with `tmp_path`, not a live database.

- **The `BlobStore` protocol (spec Appendix A) takes no session and no `tenant_id`** — `put`/`get`/`exists` only
  ever see raw bytes and a digest. Tenant-scoped visibility and the `blob` table row itself are therefore a
  concern for whichever higher-level caller uploads content (e.g. an eventual `/v1/blobs` endpoint), not for the
  store. `protocols.py` only defines `BlobStore` so far; the other Appendix A protocols (`LeanService`,
  `ModelBackend`, `Policy`, `ToolClient`, `Sink`) are left out until something actually implements or consumes
  them, since adding them now would be speculative surface area with no way to check the shape is right.
- **`store_or_inline`'s spec §5.3 64 KiB threshold is inclusive of the boundary itself** — data of exactly
  `INLINE_THRESHOLD` bytes returns `Inline`, not `BlobRef`; only content strictly *above* the threshold is
  written to the store. Tested explicitly at the boundary (`INLINE_THRESHOLD` bytes and `INLINE_THRESHOLD + 1`
  bytes), not just with clearly-small/clearly-large samples, since an off-by-one here would silently put oversized
  content into a `bytea` column instead of the CAS.
- **No async file I/O library was added.** `LocalBlobStore` wraps ordinary synchronous `Path` calls in
  `asyncio.to_thread` rather than depending on `aiofiles`, since local-disk I/O is fast enough that a dedicated
  async-file dependency isn't worth it just to satisfy the `BlobStore` protocol's `async` methods.
- **No new test-only async infra was added either.** The workspace has no `pytest-asyncio` (or similar) dependency
  — `tests/db/test_schema.py` already exercises the async-capable ORM through a synchronous driver/session rather
  than async tests. `tests/test_blobs.py` follows the same minimal-dependency approach: plain sync `def test_...`
  functions drive the async `LocalBlobStore`/`store_or_inline` API via `asyncio.run(...)`.

## Implementation notes: leanserv process-management facts

These surfaced while building M1.8.2 (`packages/leanserv/src/lean_agent_serv/repl.py`), the wrapper around one
`lake exe leankernel serve` process (M1.8.1) — found by actually spawning the real subprocess and driving it into
each failure mode, not by reasoning about `asyncio.subprocess` in the abstract. Several of these cost real debugging
time (one test hung for 67 seconds before its root cause was clear), which is exactly why they're recorded here.

- **`lake exe` does not exec-replace itself — it forks the actual compiled binary as its own child and stays
  alive supervising it.** Confirmed empirically via `ps aux` during a deliberately-hung check: two distinct PIDs
  were alive simultaneously, `lake exe leankernel serve Init` and a separate `leankernel serve Init`. This means
  signalling only the direct child (`asyncio.subprocess.Process.kill()`, targeting the `lake` PID) does **not**
  stop the actual elaboration — it orphans the real worker process, which keeps running (still burning CPU on
  whatever it was doing) and, worse, keeps its own inherited copy of the stdout pipe's write end open, so a
  reader on the Python side never even sees EOF. A test built on this wrong assumption "passed" but took 67
  seconds instead of ~1: the timeout fired correctly, but killing only `lake` left the orphaned grandchild running
  long enough that unrelated system noise eventually reaped it. **Fix**: spawn with `start_new_session=True`
  (POSIX `setsid`), which puts `lake` and everything it forks into one new process group with `lake`'s own pid as
  the group id, then signal the whole group via `os.killpg(pid, signal.SIGKILL)` instead of `process.kill()`. Any
  future code that spawns a `lake exe` subprocess and needs to be able to kill it reliably needs this same
  pattern — it is not specific to `leankernel serve`.
- **A `check` body's `#eval`-triggered `IO.println`/`IO.eprintln` output is captured into the command's message
  log and returned over *stdout* as ordinary diagnostics — it is never written to the process's real stderr fd.**
  Confirmed empirically: a body calling `IO.eprintln` thousands of times, run with stderr redirected to a file,
  left that file at zero bytes; the printed lines showed up in the JSON response's `diagnostics` array instead.
  This is Lean's own `#eval`/`#print`-output-capture mechanism (the same thing that makes `#eval`'s printed output
  show up as an info message in an editor) — not a bug in `Serve.lean`. Practical consequences: (1) a large
  volume of `#eval`-printed output shows up as elaboration *latency* (processing thousands of message-log entries
  and JSON-encoding a large diagnostics array), not as a stderr-pipe problem — an intended stderr-flood test for
  `_drain_stderr` was scrapped for exactly this reason, since it couldn't actually reach the real stderr fd at
  all; (2) real traffic on the worker's stderr pipe in production is expected to be rare (a Lean panic, a
  C-runtime message, GC diagnostics) rather than anything a normal proof attempt would trigger — `_drain_stderr`
  is warranted as defense-in-depth regardless, just not something this module's own tests can manufacture on
  demand to prove prevents a deadlock.
- **Crash taxonomy is a closed set of three, deliberately not one flat exception**: `ReplTimeout` (wallclock
  budget elapsed; the process was already SIGKILLed by the time it's raised), `ReplExited` (the process is gone
  for a reason other than our own timeout kill — crashed, OOM-killed, or exited on its own), and
  `ReplProtocolError` (the process is alive and responded, but with something that isn't a valid, correlated
  response — `Serve.lean`'s own `handleLine` guarantees a response line for every input line even a malformed
  one, so this specifically means stdout desynchronized from the request stream). All three subclass
  `ReplCrashed`, and a normal elaboration failure (`ok=False`, e.g. a type error) is **not** one of them — it's
  spec's own "`infra_error` is a distinct, unbudgeted outcome from a proof failure" principle applied one layer
  down from the obligation state machine to a single worker.
- **The response-`id`-mismatch branch of `ReplProtocolError` is reproducible for real, not just by construction**:
  writing an extra, well-formed request line directly to the process's stdin ahead of a normal `check()` call
  genuinely desynchronizes the pair, since `Serve.lean` answers every line strictly in order — `check()`'s own
  `readline()` then reads that stray response first. `tests/leanserv/test_repl.py` exercises this directly rather
  than asserting the branch only by code inspection.
- **No `pytest-asyncio` dependency here either**, continuing M1.7's precedent: `tests/leanserv/test_repl.py`'s
  `def test_...` functions are plain sync functions driving `ReplWorker`'s async API via `asyncio.run(...)`.
- **`tests/leanserv/` lives in CI's `lean` job, not the `python` job.** It spawns the real `leankernel serve`
  binary (never a mock), which needs the Lean toolchain and Mathlib already set up — putting it in the `lean` job
  reuses that job's own toolchain setup and cache instead of duplicating an expensive elan+Mathlib install into
  the `python` job for a handful of tests. Local dev mirrors `tests/db/`'s Postgres-connectivity convention: the
  suite skips gracefully (not a fake pass) if `packages/leankernel`'s built exe isn't found, and CI always builds
  it first (via `lean-action`, before `tests/leanserv` runs) specifically so that skip never triggers there.

## Implementation notes: pool facts

These surfaced while building M1.8.3 (`packages/leanserv/src/lean_agent_serv/pool.py`), the base-env-keyed
`LeanReplPool` over M1.8.2's `ReplWorker` — tested against real spawned processes throughout, including deliberately
crashing one mid-test to confirm the pool's own bookkeeping reacts correctly, not just its happy path.

- **Mutual exclusion is the pool's actual reason to exist, not merely reuse.** `LeanKernel.Serve`'s wire protocol
  (M1.8.1) is strictly one request in, one response out, in order — sending two concurrent `check`s to the *same*
  process desynchronizes it exactly the way M1.8.2's `ReplProtocolError` detects. `acquire`/`release` give each
  in-flight check sole ownership of one worker for its duration; `warm_per_base_env` (not a single worker per
  base env) is what lets concurrent requests against the *same* base env avoid serializing on one process.
- **A worker's own `is_alive` is the only "should this be reused" signal `release` needs — no separate
  success/failure flag has to be threaded through the pool.** `ReplWorker.is_alive` already reflects every crash
  path M1.8.2 defines (timeout, exit, protocol desync), and an ordinary elaboration failure (`ok=False`, ownership
  still intact) leaves a worker just as alive and reusable as a success. Confirmed by deliberately timing a
  worker out mid-test, then releasing it: the pool's `idle_workers` count stayed at zero rather than re-admitting
  a worker that had already been SIGKILLed.
- **An idle worker can die between being released and being handed back out** (crashed on its own, or reaped by
  something else) — `acquire`'s reuse loop checks `is_alive` on every candidate it pops from the idle list and
  discards a dead one rather than handing it out, instead of trusting idle-list membership alone as proof of
  liveness.
- **LRU eviction only ever frees one slot per call, at base-env granularity, and is a deliberate no-op if nothing
  is evictable** — it evicts one idle worker belonging to the single least-recently-used base env (excluding the
  one currently being served), not an arbitrary count or the worker being requested. If every base env with idle
  capacity is either the one being served or has no idle worker to give up, `acquire` simply spawns over the soft
  `max_total_workers` cap rather than blocking forever waiting for room that will never appear — the cap is
  enforced by eviction pressure, not as a hard invariant on `total_workers`.
- **A `base_env_key`'s imports are validated for stability at every `acquire`, not just recorded once and
  forgotten.** Two different recipes claiming the same key is a caller bug (violates the entire premise that a
  base-env identity means one specific, stable environment) — `acquire` raises `ValueError` rather than silently
  keeping whichever recipe it saw first or silently switching to the new one, either of which would hide the bug
  instead of surfacing it immediately at the call that introduced the inconsistency.
- **No `pytest-asyncio` dependency here either**, continuing the pattern from M1.7/M1.8.2: `tests/leanserv/
  test_pool.py`'s `def test_...` functions are plain sync functions driving `LeanReplPool`'s async API via
  `asyncio.run(...)`.

## Implementation notes: cache/verdict facts

These surfaced while building M1.8.4 (`packages/leanserv/src/lean_agent_serv/{cache,verdicts}.py`), the first
real caller of M1.7's `store_or_inline` — tested against a live Postgres, connected as the actual `leanserv` role
(never the admin/superuser) for every write the production code path would make.

- **A blob-suffixed `bytea` column can hold either inline content or a CAS digest, and nothing about the column
  itself says which** — M1.7's `store_or_inline` returns a distinct `Inline`/`BlobRef` in memory, but that
  distinction evaporates the moment either gets written into the same untyped `bytea` column, and a 32-byte
  inline value would be indistinguishable from a digest by length alone. `lean_agent_core.blobs` gained
  `to_bytea`/`from_bytea` in this milestone specifically to close that gap: a one-byte tag prefixed onto the
  actual bytes, entirely within the existing column type, no migration needed. This was a real, load-bearing gap
  in M1.7's own design that nothing surfaced until M1.8.4 became the first real consumer — worth remembering that
  "the caller writes the digest into the column instead" (M1.7's own phrasing) was only half a design.
- **Reusing one `async_sessionmaker`/engine across two separate `asyncio.run()` calls in the same test raises
  "Future attached to a different loop."** asyncpg connections are bound to the event loop that created them;
  `asyncio.run()` tears its loop down on return, so a second `asyncio.run()` reusing the same pooled connection
  hits a live connection whose loop no longer exists. Confirmed empirically (a first draft of the L1-survives-a-
  fresh-store test called `asyncio.run` twice against the same fixture and failed exactly this way) — the fix is
  always one `asyncio.run(run())` wrapping every use of a given engine within one test, never two sequential ones.
- **Proving an L0 (in-process) hit never reaches Postgres needs more than `engine.dispose()`.** Disposing an
  `AsyncEngine` only discards its current pooled connections; the engine transparently opens a fresh one on next
  use, so a disposed-then-reused engine would still "work" and silently defeat the test's own point. The actual
  proof: swap the store's session factory for one pointed at a host that cannot resolve (`host.invalid`) and
  confirm `get()` still returns the correct cached value — anything reaching Postgres through that factory would
  fail immediately, not slowly succeed.
- **`VerificationCacheStore.put` is idempotent by design, not merely by accident** — two workers computing the
  same content-addressed `cache_key` concurrently is the expected case, not a race to detect and prevent, so
  `put` uses `INSERT ... ON CONFLICT (cache_key) DO NOTHING` rather than an upsert or a pre-check-then-insert.
  `VerdictWriter.write`, in contrast, deliberately does **not** do this — `verdict.attempt_id` being the primary
  key with no conflict handling is intentional (spec: at most one verdict per attempt, ever), so a second write
  for the same attempt must surface as a genuine `IntegrityError`, not be quietly absorbed.
- **`sealed_obligation` (and the admin-engine/role-URL fixtures under it) moved from `test_privileges.py` into a
  shared `conftest.py`** once a third and fourth file (`test_cache.py`, `test_verdicts.py`) needed the exact same
  genuinely-committed-obligation fixture data. Plain (non-fixture) names from a `conftest.py` are importable via a
  normal `from conftest import ...` from any test file whose own directory pytest has added to `sys.path` (its
  default "prepend" import mode does this per test file) — confirmed empirically by running the suite after the
  move, not assumed from pytest's documentation alone. This `conftest.py` moved a second time in M1.8.5, from
  `tests/db/` to top-level `tests/` — see that milestone's own note below for why a sibling directory wasn't
  enough once a fourth consumer needed it from outside `tests/db/` entirely.

## Implementation notes: FastAPI surface facts

These surfaced while building M1.8.5 (`packages/leanserv/src/lean_agent_serv/api.py`), the last `leanserv`
sub-milestone — wiring `pool.py`/`cache.py`/`verdicts.py` behind `/v1/check`, `/v1/check_batch`, `/v1/health`,
tested through a real `FastAPI` app against a real spawned Lean process and a real Postgres, never mocked.

- **`/v1/seal`, `/v1/link`, `/v1/replay`, `/v1/decompose`, and `/v1/base-env/materialize` are not built.**
  `LeanKernel.Serve`'s wire protocol (M1.8.1) only ever understood one request kind, `check` — there is no
  seal/link/replay/decompose request shape on the Lean side for an HTTP route to dispatch to, and a route with
  nothing real underneath it is exactly the half-finished surface this project avoids. `/v1/link` is the one that
  most wants `VerdictWriter` (M1.8.4) as a caller (spec: "then replay and audit; writes the verdict row") and is
  the natural next step once `Serve.lean` grows a `link` request kind to go with it — not part of this milestone.
- **A bare `check`'s outcome is classified with the same `VerdictKind` enum a proof verdict uses**, not a
  separate ok/not-ok shape: `ok=True` (clean elaboration) is `PROVED`, a genuine elaboration error is `ERRORS`, a
  `ReplTimeout` is `TIMEOUT`, and any `ReplCrashed` (`ReplExited`/`ReplProtocolError`) is `INFRA_ERROR` — one more
  application of spec's own "infra_error is a distinct, unbudgeted outcome from a proof failure" principle, this
  time one layer down from the obligation state machine to a single check. `REFUTED` and `OOM` are never produced
  here: refutation isn't what a bare check does, and nothing in this module's crash taxonomy can reliably tell an
  OOM kill apart from any other external kill.
- **`INFRA_ERROR` results are never written to the cache; `TIMEOUT` results are, deliberately.** A crash is not
  informative about the checked content (the same body might succeed cleanly on healthy infra next time), so
  caching it would poison future attempts against transient infrastructure trouble. A timeout, by contrast, is
  exactly one of the `verdict_kind` values spec's own `verification_cache` schema stores — treated as a
  reproducible property of content-plus-environment, not a fluke, and cached like any other outcome.
- **A cache hit skips the `base_env` database lookup entirely, not just the Lean check.** `compute_cache_key`
  only needs the raw digest bytes (spec's own formula: `sha256(base_env_digest ‖ sha256(source) ‖
  canonical(options))`), so `_run_check`/`_run_check_batch` check the cache *before* resolving `base_env_digest`
  to a recipe — the (comparatively expensive) database round trip to look up imports only happens on an actual
  miss.
- **`/v1/check_batch`'s shared-worker degradation needs no special-case code.** If the one worker acquired for a
  batch crashes partway through, every subsequent item in the batch simply calls `ReplWorker.check` on an
  already-dead worker, which (per M1.8.2) immediately raises `ReplExited` without attempting to use it — each
  remaining item comes back `INFRA_ERROR` on its own, cheaply, rather than the batch aborting or silently
  skipping the rest of the list.
- **Diagnostics round-trip through the cache as a JSON array, not newline-joined text.** A Lean diagnostic
  message can itself contain newlines, so storing `"\n".join(diagnostics)` and splitting on `"\n"` to read it
  back would be lossy and ambiguous about where one message ends and the next begins. `CachedCheck.messages`
  holds `json.dumps(list(diagnostics)).encode()` instead, decoded the same way on a hit.
- **`tests/leanserv/test_api.py` needs both the Lean toolchain and a live Postgres at once** — the only test file
  in the repo with that combination (`test_repl.py`/`test_pool.py` need only Lean; every `tests/db/` suite needs
  only Postgres). Rather than adding the (expensive) Lean toolchain/Mathlib setup into the `python` CI job, a
  Postgres service container (cheap) was added to the `lean` job instead — see `.github/workflows/ci.yml`. This
  is also what forced the `conftest.py` holding `admin_engine`/etc. up from `tests/db/` to top-level `tests/`: a
  sibling directory's `conftest.py` is invisible to pytest's fixture lookup, only an ancestor's is, and
  `tests/leanserv/` and `tests/db/` are siblings, not one an ancestor of the other.
- **FastAPI's `TestClient` needs no `asyncio.run` gymnastics at all**, unlike every other async module in this
  package. It manages its own event loop/portal for its whole `with TestClient(app) as client:` context, so
  ordinary `client.post(...)`/`client.get(...)` calls inside a plain sync `def test_...` function work directly —
  simpler than the `asyncio.run(...)`-per-test pattern `test_repl.py`/`test_pool.py`/`test_cache.py` all use for
  their own directly-async APIs.
- **A pool constructed outside `TestClient` and torn down afterward via a separate `asyncio.run(pool.aclose())`
  silently leaked every worker process — found only by checking `ps aux` immediately after a full test run, not
  by anything failing.** `TestClient` runs the whole app — every route handler's `await`s, and therefore every
  `ReplWorker` subprocess `pool.py` spawned — on its own internal event loop/portal. Calling
  `pool.aclose()` afterward from a *different* loop hits `asyncio.subprocess`'s version of the "Future attached
  to a different loop" error M1.8.4 already found for asyncpg — except here `LeanReplPool.aclose()`'s
  `asyncio.gather(..., return_exceptions=True)` swallows the error silently, so `_kill()` never actually ran and
  every worker kept running, undetected, until Python's own garbage collection eventually closed their pipes
  (confirmed empirically: they died on their own within ~15 seconds, not immediately, and not via anything this
  codebase's own cleanup code did). **Fix**: `create_app` now closes `pool` itself, via FastAPI's `lifespan`
  parameter — `lifespan` runs on ASGI shutdown, which `TestClient.__exit__` triggers on the *same* loop the app
  (and its workers) ran on the whole time. `engine.dispose()` (the database engine, disposed separately by
  whatever constructed it) does *not* need the same fix — confirmed empirically by checking Postgres's own
  connection count before and after, which returns to baseline either way; `AsyncEngine.dispose()` mainly closes
  idle pooled connections rather than interacting with an in-flight Task/Future tied to the old loop, which is
  the specific shape of operation that breaks across a loop boundary.
- **This also fixed a latent bug in `ReplWorker._kill`/`close` that the cross-loop issue above was masking.**
  `_kill` used to early-return if `self._process.returncode` (the direct `lake` child) was already set, skipping
  `os.killpg` entirely — but a POSIX process group survives as long as *any* member does, independent of whether
  the original leader (`lake`) has already exited, so this early return could leave an orphaned `leankernel`
  grandchild alive even when `_kill` *was* correctly reached. `close()`'s graceful path only ever awaited
  `self._process.wait()` (the direct child), which can return before the grandchild has actually finished exiting
  on its own. Both are fixed the same way: `_kill` always calls `os.killpg` (catching `ProcessLookupError` as the
  only genuine no-op case), and `close()` calls `_kill()` unconditionally at the end, not only on its timeout path.

## Implementation notes: eval harness facts

These surfaced while building M1.9 (`packages/eval/src/lean_agent_eval/{score,reverify,contamination}.py` and
`suites/internal.py`), the "eval harness skeleton" spec's Phase 1 description names as a deliverable alongside
`leankernel`/`leanserv`/DDL/blob store. "Skeleton" is load-bearing here: Phase 2/3/4 (control loop, models,
policies) don't exist yet, so this milestone builds the scoring/reverification *machinery*, tested with synthetic
or hand-authored data, not an end-to-end benchmark run against a real prover.

- **`lake check --paranoid`, spec's own literal wording for reverification, does not exist.** `lake --help`
  (Lake 5.0.0-src, the version v4.33.1's toolchain pins) lists `build`/`test`/`env`/`lean`/... but no `check`
  subcommand at all, and no `--paranoid` flag anywhere. What actually re-verifies a file from scratch is
  `lake env lean <file>`: elaboration *is* kernel type-checking in Lean (M1.1's own established finding — there is
  no separate lighter "check-only" mode), so a plain `lake env lean <file>` genuinely re-verifies, and its exit
  code is reliable (confirmed against both a real passing and a real failing file). "Paranoid" already means
  something specific and different elsewhere in this codebase's own vocabulary (`LinkRequest.paranoid`, spec's own
  field for multi-kernel replay; Appendix B's `paranoid_replay` config knob) — neither implemented yet, since
  `/v1/link` itself doesn't exist (M1.8.5 deferred it).
- **`lake env`, not just `lake exe`, forks a real child process rather than exec-replacing itself.** Confirmed
  empirically the same way M1.8.2 found this for `lake exe` (`ps aux` during a deliberately-hung `lake env lean`
  showed two live PIDs, `lake env lean <file>` and a separate `lean <file>`). `reverify.py`'s timeout path uses
  the identical fix: `start_new_session=True` at spawn, `os.killpg` on timeout, never a plain `process.kill()`.
  Worth restating because it is easy to assume a *different* `lake` subcommand behaves differently without
  checking — it doesn't, and the fix generalizes to "any `lake` subcommand this codebase spawns," not just the
  one M1.8.2 happened to hit first.
- **A `sorry`-as-proof development elaborates with `ok=True` through plain `check`, and this is correctly
  documented as a known limitation, not silently wrong.** `check` (M1.8.1's `Serve.lean`) is elaboration only —
  it has no axiom audit, and `sorryAx` produces only a message-log *warning*, never an error (M1.1's own finding).
  The internal suite includes a `sorry`-as-proof problem specifically to pin this down as a regression test: if a
  future change to `check` started auditing axioms (or started erroring on `sorryAx`), this test's expectation
  would need to change too, which is exactly the point of encoding today's actual behavior in `EXPECTED_OK` rather
  than asserting what a full seal/link/replay/audit acceptance check would eventually do.
- **`INFRA_ERROR` (and a missing verdict, `kind=None`) are excluded from pass@k's own `n`/`c` count, but still
  counted in the reported budget totals.** Spec's "infra_error is a distinct, unbudgeted outcome from a proof
  failure" principle, applied to scoring specifically: an infra crash says nothing about whether the *content* is
  provable, so folding it into `n` (as an implicit failure) would understate a policy's real capability. But the
  wallclock/tokens/kernel-ms an infra_error attempt burned were still genuinely spent, so `BudgetTotals` sums
  every outcome, not just the ones that count toward `n`/`c` -- reported "pass@k with the budget that produced
  it" (spec's own phrasing) means the full cost, including waste, alongside a capability number that isn't
  distorted by that waste.
- **Contamination checking is exact-match-after-normalization only, deliberately, not fuzzy/semantic matching.**
  Semantic detection needs a model or embeddings, and this codebase has none yet (Phase 3 doesn't exist) --
  building fuzzy matching now would mean faking that dependency. Exact match catches the common, cheap-to-miss
  case (a benchmark statement copied verbatim, differing at most in whitespace/comments) without pretending to
  solve paraphrase-level contamination this milestone has no way to test against the real thing it's for.
- **The internal regression suite runs through `ReplWorker` directly, not `pool.py`/`cache.py`/`api.py`.** It's
  meant to be the cheapest possible "did the core acceptance mechanism regress" signal (spec: "every PR"), and
  the full pool/cache/API stack needs Postgres — `tests/leanserv/test_api.py` already exercises that stack for
  real; duplicating it here for a suite meant to run on every PR would add an unnecessary Postgres dependency to
  the one suite that should need the least infrastructure to run.

## Sequencing constraints (spec §8)

The MVP roadmap is ordered by *what cannot be safely retrofitted*, not by what's easiest. The acceptance path
(seal/link/replay/audit) and the full obligation/attempt/verdict schema come before anything that uses them,
including before any model is wired in — Phase 2 ("null agent") makes zero model calls and must close the easy
tail of miniF2F deterministically at zero token cost, because it's the only way to later distinguish a broken
harness from a policy that needs tuning. If asked to add a feature, check the MVP/post-MVP tables in §8–9 before
assuming it belongs now — Ray, MCP wrappers, the zygote fork, `BestFirstDAG`, multi-tenancy, and the training
loop are all explicitly deferred, and pulling one forward without its prerequisites is a design smell, not a
convenience.
