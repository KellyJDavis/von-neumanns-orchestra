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
`verdicts.py`), M1.8.5 (`api.py` FastAPI surface: `/v1/check`, `/v1/check_batch`, `/v1/health` — `/v1/link`,
`/v1/replay`, `/v1/decompose`, and `/v1/base-env/materialize` are deliberately not built yet; see
`api.py`'s own module docstring and the implementation notes below for why). M1.9 (eval harness skeleton --
`packages/eval`: `score.py`, `reverify.py`, `contamination.py`, the internal regression suite) is also complete.
Gate 7 (decomposition round-trip on a Mathlib sample) is also done — `LeanKernel/DecomposeFuzz.lean`, run
periodically/manually at the gate's own named scale via `lake exe leankernel decompose-fuzz <seed> <count>` (not
part of `kernel_tests`/every-commit CI — see the implementation notes below for why even a small slice doesn't
fit CI's actual budget). Gate 9 (prelude memory delta, R19's input) is also done —
`packages/leanserv/src/lean_agent_serv/memory_probe.py`; measured result: a prelude's own declaration content
costs on the order of ~2.4 KiB/declaration against an already-warm base (clean signal only above ~10,000
declarations — smaller sizes are invisible against ~2 MiB of run-to-run RSS jitter in a ~1.5 GiB warm Mathlib
worker), which is small next to the ~1.5 GiB the base import itself costs; see the implementation notes below for
the full readout and what it means for R19.

Phase 2 (the "null agent" — zero model calls, closing miniF2F's easy tail deterministically) has begun. M2.1
extends the acceptance path onto the wire, one request kind at a time: M2.1.1 (`/v1/seal` — `Serve.lean`'s `seal`
kind, `ReplWorker.seal`, `POST /v1/seal`, `lean_agent_core.digests`) and M2.1.2 (`/v1/link` — the whole
acceptance path on one submission: link + replay + audit, writing the `verdict` row) and M2.1.3
(`/v1/decompose` — `sorry` extraction into closed standalone statements) are done, completing M2.1.
M2.2 (the obligation state machine — `lean_agent_core.state` plus the `SECURITY DEFINER` transition
functions and cycle-guard trigger in `deploy/grants.sql`) and M2.3 (scheduler — `lean_agent_core.scheduler`:
claim, lease, heartbeat, reaper) M2.4 (control loop — `lean_agent_core.worker`) and M2.5 (Policy/Action, the executor, and
`SymbolicPortfolio` — the null agent proves real Lean goals with zero model calls) and M2.6 (ingestion —
`lean_agent_api.ingestion`: submission → sealed goals → obligations, with §4.5 admission signals) and M2.7's
bundle materialization (`lean_agent_api.materialize`) are done — **the whole Phase 2 pipeline now runs end to
end**: submission → ingestion → materialization → claim → null agent → link/replay/audit → `mark_proved`, with
zero model calls, and out the other side as a **standalone materialized `.lean` file** with its `sorry`s filled
(spec §6.3 step 6) — `tests/leanserv/test_end_to_end.py`. M2.8 (the public §6.1 API surface —
`lean_agent_api.app`), M2.9 (`lean_agent_cli`, an httpx client of that API, plus
`lean_agent_serv.client.LeanServiceClient`) and M2.10 (the miniF2F exit gate --
`packages/eval`'s `suites/minif2f.py` over a vendored, digest-pinned corpus, run by
`tests/eval/test_minif2f.py`) are done. **Phase 2 is complete**: its exit criterion holds -- the
null agent closes miniF2F's easy tail through the whole acceptance path at zero token cost, stable
across three runs, with a materialized file that elaborates and links. Phase 3 (model as policy)
is next; note its own exit criterion begins "the Phase 2 symbolic baseline still passes
bit-identically", which is what that gate is now for.

Phase 1's remaining scope is gate 1's 10k-proof throughput report,
blocked on the Lean Workbook corpus targeting the wrong toolchain version — unresolved since Phase 1 planning —
not new modules. See "Implementation notes" below for load-bearing facts discovered while building all of the
above. Read
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
- Lean (`packages/leankernel`): `lake build` (builds `LeanKernel` + links the `leankernel` exe — both are
  `@[default_target]`, which the exe deliberately is; see the leanserv note on why), `lake test` (builds and runs
  `kernel_tests`, the Audit/Seal test suite — this is what CI's `lean` job runs via `lean-action`'s `test: true`).
  Run `lake exe kernel_tests` directly for a single test run without going through `lake test`'s dependency
  check.
- `leankernel serve` (M1.8.1/M2.1.1–M2.1.3, the persistent warm-environment process `leanserv`'s pool spawns one
  of per worker): `lake exe leankernel serve [<import>...]` (e.g. `serve Init` for a fast Mathlib-free session, or
  `serve Mathlib.Algebra.Group.Defs` for a real Mathlib base env), then write newline-delimited JSON requests to
  its stdin — e.g. `printf '{"id":"1","body":"def foo : Nat := 5"}\n' | lake exe leankernel serve Init` for a
  `check`, or `printf '{"id":"1","kind":"seal","goals":[{"name":"G","statement":"True"}]}\n' | lake exe
  leankernel serve Init` for a `seal` — and read one JSON response line per request from stdout. Closing stdin
  exits it with code 0.
- Driving a `link` request by hand needs a *materialized* bundle on the search path, since Link requires the
  sealed goal to be an imported constant. Compile one with
  `lake env lean --root=<dir> <dir>/LeanAgent/Goals/Bundle_<sha>.lean -o <dir>/LeanAgent/Goals/Bundle_<sha>.olean`
  (`--root` is required: `lake env lean` refuses a file outside the package root otherwise), then
  `LEAN_PATH=<dir> lake exe leankernel serve Init LeanAgent.Goals.Bundle_<sha>` and send
  `{"id":"1","kind":"link","goal":"LeanAgent.Goals.G_x","entry":"LeanAgent.Sol.sol","body":"<development>",
  "allowAxioms":["propext","Classical.choice","Quot.sound"]}`.
- `leankernel decompose-fuzz` (gate 7's periodic/manual validation run — **not** part of `kernel_tests`/`lake
  test`; see the implementation note below on why even a small per-commit slice doesn't fit CI's actual budget):
  `lake exe leankernel decompose-fuzz <seed> <count> [<import>...]` (imports default to `Mathlib` if none given)
  — samples `<count>` real theorems from the imported modules and round-trip-checks each (spec's gate 7: children
  link standalone, reassembly links against the parent, no new axioms), printing a JSON `{sampleSize, passed,
  failures}` report and exiting nonzero if `failures` is non-empty. At the gate's own named scale (5,000-10,000),
  budget 20-35 minutes (measured: ~213ms/declaration locally). Run manually/periodically, not on every commit.
- `memory_probe` (gate 9's periodic/manual validation run — not part of the automated test suite, for the same
  "measurement, not a pass/fail assertion" reason as `decompose-fuzz`): `uv run python -m
  lean_agent_serv.memory_probe --prelude-import <Module> [--prelude-import <Module> ...] [--base-import <Module>
  ...] [--repeats N]` (base defaults to `Mathlib.Algebra.Group.Basic`) — for each `--prelude-import`, spawns a
  paired `(base, base+prelude)` worker measurement `--repeats` times and reports the mean RSS delta. Needs a real
  Postgres-free, Lean-toolchain-only setup, but note that a plain `lake build` in `packages/leankernel` does
  *not* build the `LeanKernelTests.SyntheticPrelude{50,1000,10000}` fixtures used as example preludes -- they
  aren't imported by anything `lake build`'s default targets reach, so `lake build LeanKernelTests` (the whole
  library, not just its default target) is what actually compiles them before a run using them as
  `--prelude-import`. See the implementation
  notes below for the actual measured numbers and why `--repeats` (and a large enough prelude) matter.
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
  `psql postgresql://postgres:postgres@localhost:5432/leanagent -f deploy/grants.sql` — this file holds both
  the role grants, every obligation-status transition function (M2.2), and the scheduler's claim/reap functions
  (M2.3), since the three are one design: `app` cannot write `obligation.status`, so these functions are the
  only way it moves. Note the plain
  `postgresql://` URL, not `DATABASE_URL`'s `postgresql+asyncpg://` (psql/libpq don't understand the SQLAlchemy
  driver suffix). It's idempotent (safe to re-run). CI applies it via `psycopg` instead of the `psql` binary, so
  it doesn't depend on a Postgres client being preinstalled on the runner — see `.github/workflows/ci.yml`. Then
  `tests/db/test_privileges.py` exercises it (gate 8) the same way `test_schema.py` exercises the schema, and
  `tests/db/test_state.py` (M2.2) exercises every transition function and the cycle-guard trigger as the real
  `app` role.
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
  `uv run pytest tests/leanserv`. The `lake_project_dir` fixture every Lean-spawning suite uses lives in the same
  top-level `tests/conftest.py`; it skips when the exe isn't built, unless `LEANKERNEL_REQUIRED=1` (which CI's
  `lean` job sets) makes that a hard failure instead — see M2.1.1's note on why that guard exists. That file also
  holds `materialized_bundle` (M2.1.2), which compiles a real sealed bundle with `lake env lean` — `/v1/link`
  cannot be exercised without one, since Link requires the goal to be a genuinely imported constant.
- `packages/eval` (M1.9, eval harness skeleton): `score.py`/`contamination.py` are pure logic (`uv run pytest
  tests/eval/test_score.py tests/eval/test_contamination.py`, no external infra); `reverify.py` and the internal
  regression suite (`suites/internal.py`) spawn real Lean processes, same `lake build` prerequisite and skip
  convention as `tests/leanserv/` — run all of it with `uv run pytest tests/eval`.
- miniF2F, Phase 2's exit gate (M2.10, `packages/eval`'s `suites/minif2f.py`, run by
  `tests/eval/test_minif2f.py`): needs **full Mathlib built** (`lake build` in `packages/leankernel` is not
  enough — nothing imports all of Mathlib at compile time; use `lake exe cache get`, which is what CI's
  `lean-action` does), plus a live Postgres with grants applied. It skips locally when `Mathlib.olean` is absent
  and hard-fails instead under `LEANKERNEL_REQUIRED=1`. Budget ~2 minutes: a full-Mathlib worker takes ~30 s to
  warm and ~6 GiB, so the pool is capped at one worker deliberately (see the notes below).
  Two manual tools sit beside it, neither run by CI: `uv run python -m lean_agent_eval.suites.vendor_minif2f`
  rebuilds the vendored corpus from a pinned upstream commit, and `uv run python -m
  lean_agent_eval.suites.survey_minif2f --out <path>` re-measures which of the 488 problems the null agent
  closes (~25 min) — that measurement is what `EASY_TAIL` is derived from, and it is checked in rather than
  recomputed by the gate.

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
- **Gate 7 does not run in `kernel_tests`/`lake test` at all, even at a small sample size.** First attempt added a
  30-declaration slice — comfortably fast locally (the whole binary ran in ~27s) — but CI's `lean` job failed
  twice with "The operation was canceled." Reducing the slice to 5 declarations did *not* fix it (still
  cancelled, now partway through a *faster* test run), which was the clue that the constraint was never time:
  see the memory finding below, which the gate-7 symptom matches exactly (a Mathlib-bearing environment added to
  an already near-the-limit suite). The removal stands on its own cost grounds regardless: gate 7 is a
  periodic/manual run by design (see the ~213ms/declaration note above), and `lake exe leankernel decompose-fuzz`
  is its only exercise mechanism.
- **What cancels that job is memory, not time — the `lean` job's runner gets killed when
  `kernel_tests`' peak footprint approaches the runner's 16 GiB.** Two earlier diagnoses were both
  wrong and are recorded here so they are not re-derived: it is *not* a fixed 180s workflow cap (the `lean` job
  has since completed successfully at 266s, and one of the cancelled gate-7 runs was cancelled at 260s), and it
  is not a per-check *time* cost to size around. The signature that gave it away, on M2.1.2's first CI run:
  `##[error]The runner has received a shutdown signal ... Process completed with exit code 143` — SIGTERM to the
  whole runner mid-`lake test`, which surfaces in the UI as the generic "The operation was canceled."
  Measuring locally (`lake env /usr/bin/time -l ./.lake/build/bin/kernel_tests`) showed **20.9 GiB peak RSS**
  against a 16 GiB runner. **Root cause**: `LeanKernelTests/Goals.lean` carried a gratuitous `import Lean` — two
  ordinary `def`s that need nothing from it — so every test elaborating against that module loaded a whole extra
  copy of Lean's environment. Changing it to `import Init` took the suite to **9.5 GiB**, a 2.2x cut, with all 60
  checks still passing. Generalizes: **a test fixture's `import` line is a memory decision, not just a
  dependency declaration**, because each `importModules`/`processHeader` call in this suite materializes a live
  environment, and several are alive at once. Prefer `import Init` in any fixture that does not genuinely need
  more — it is also the more faithful fixture, since a real sealed bundle imports its base environment, never
  `Lean`. When a CI job is killed rather than failing, check peak memory before reaching for timeouts:
  `gh api repos/<owner>/<repo>/actions/jobs/<id>/logs` shows the exit code and signal, and
  `/usr/bin/time -l` (macOS) or `/usr/bin/time -v` (Linux) reproduces the footprint locally.
  `leanprover/lean-action`'s own source has no timeout of its own, which is still true and still means
  `timeout-minutes` in `ci.yml` is not what governs this.

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

## Implementation notes: obligation state machine facts (M2.2)

These surfaced while building `lean_agent_core.state` and the transition functions in `deploy/grants.sql`,
against a real PostgreSQL 16 driven as the real `app` role.

- **Spec §6.4's claim SQL cannot run under spec §5.5's own grant list, and §5.5 wins.** §6.4 writes the claim as
  a bare `UPDATE obligation o SET status = 'in_progress'`, but §5.5 grants `app` UPDATE on only
  `(priority, spent_attempts, spent_tokens, spent_kernel_ms, updated_at)` — `status` is not among them, and a
  direct update as `app` fails with "permission denied for table obligation" (confirmed against the live
  database before designing anything on top of it). So **every** status transition is a `SECURITY DEFINER`
  function, generalizing the pattern `mark_proved` already established, rather than the grants becoming broader.
  The alternative — granting `UPDATE(status)` and adding a trigger rejecting the single value `'proved'` — was
  rejected as a deny-list over values, which is the thing this codebase refuses everywhere a check decides
  whether something is trusted.
- **Each transition function re-derives its own precondition from committed rows.** `mark_failed` checks
  `spent_attempts >= budget_attempts` itself; `mark_blocked` computes "every group has a dead child" itself;
  `mark_decomposed` requires the group's edges to already exist. This is what makes them enforcement rather than
  convenience, and it is the same principle as `verdict` being leanserv-only: a worker asks for a transition and
  observes the outcome, never asserts that one is warranted.
- **`release_obligation` takes a `p_charge` boolean instead of being two functions**, and does the charge in the
  same statement as the status change. Two functions would duplicate the idempotence logic for no gain; charging
  separately would leave a window where the obligation is `open` with the attempt uncharged, and the scheduler
  can re-claim it there and overspend the budget. `infra_error` passes `false` — spec's first-class *unbudgeted*
  outcome, where charging would let a flaky node quietly lower the reported pass rate.
- **"Every group has a failed child" is not "a child failed", and the difference is the whole point of competing
  decomposition groups.** A parent is blocked only when *no* group can still succeed (§4.5: "a failed reassembly
  fails only that decomposition group, and proved children remain reusable by a competing group"). A parent with
  no groups at all is explicitly *not* blocked — it is simply not decomposed — and treating "no groups" as "all
  groups dead" would block every obligation the moment anything looked at it.
- **A Python mirror of an enforcement boundary needs a test that holds it to the original, in both directions.**
  `TRANSITIONS` exists so a control loop can decide *which* transition to ask for; `tests/db/test_state.py`
  drives every legal pair against the real functions (must land where the table says) and every illegal pair
  (must not change the status). Without the second half the table could be trimmed to nothing; without the first
  it could be widened arbitrarily.
- **The right property for an "illegal transition" test is "does not change the status", not "raises".** The
  first version demanded an exception from every illegal pair and immediately failed on
  `decomposed --decomposed-->`, which the SQL accepts as a deliberate no-op because competing groups arriving for
  an already-decomposed parent are normal. Several transitions are no-ops rather than errors when they arrive
  late or twice (a release from an attempt that lost the race to one that proved the obligation). Demanding an
  exception encoded an assumption about the *implementation* where the test should have asserted the
  *guarantee*.
- **The cycle guard is a `BEFORE INSERT` trigger on `obligation_edge`, not an application check.** Spec asks for
  it "in the same transaction that inserts an edge", which a trigger satisfies by construction, and `app` holds
  `INSERT` on `obligation_edge` directly, so anything application-side would be advisory. It walks the whole
  ancestor chain with a recursive CTE (a grandchild reintroducing its grandparent's `goal_digest` is the same
  cycle one level out), rejects a self-edge outright, and enforces `run.max_depth` — the unconditional backstop
  for a cycle spelled two different ways, which `goal_digest` equality cannot catch.
- **`SQLAlchemy` masks the password when rendering an engine's URL** (`str(engine.url)` gives `***`), so
  deriving a connection string from an existing engine silently produces one that cannot authenticate. Two tests
  here failed that way before switching to the role-URL fixtures in `tests/conftest.py`; the error surfaces as an
  authentication failure, which looks like a configuration problem rather than a code one.
- **An asyncpg engine must be created *and disposed* inside the same `asyncio.run`.** M1.8.4 recorded the
  creation half; the disposal half bites separately and later. A fixture that built engines lazily and disposed
  them after the test passed every short test and failed the one making enough round trips to keep a connection
  checked out, with "Event loop is closed" raised from inside `dispose()`.

## Implementation notes: scheduler facts (M2.3)

These surfaced while building `lean_agent_core.scheduler` and the `claim_attempt`/`reap_expired_attempts`
functions, against a real PostgreSQL 16 driven as the real `app` role.

- **A `VOLATILE` function called in a `WHERE` clause runs once per scanned row, and for `claim_attempt` that
  meant claiming an obligation per row of `attempt`.** The first version returned a bare `uuid` and was read
  back as `SELECT ... FROM attempt a WHERE a.id = claim_attempt(...)`, which looks like one call and is not:
  every row the scan touched triggered another claim, marking unrelated obligations `in_progress` and opening
  attempts nobody was working. The fix is `RETURNS TABLE (attempt_id, obligation_id, run_id)` and
  `SELECT ... FROM claim_attempt(...)`, which is also one round trip rather than a claim plus a read-back
  against a table other workers are concurrently writing. Caught immediately by the tests (every claim test
  failed at once), but worth naming: this is not a Postgres quirk, it is what `VOLATILE` means, and the
  wrong form reads perfectly naturally.
- **`CREATE OR REPLACE FUNCTION` cannot change a function's return type** ("cannot change return type of
  existing function"), so the fix above needed an explicit `DROP FUNCTION` against any database that already
  had the old signature. `grants.sql` is otherwise re-appliable, and this is the one edit that breaks that
  property — noted in the file itself. Adding a parameter is a different trap in the same family: it creates a
  new *overload*, leaving the old signature callable.
- **The heartbeat is a thread with its own connection, and there is a test that fails if it ever becomes an
  asyncio task.** Spec's reason ("a blocking tokenizer call or CPU-bound serialization inside a policy would
  otherwise starve it and get a live worker reaped") is the kind of claim that is easy to honour in shape and
  lose in substance, so `test_heartbeat_keeps_beating_while_the_event_loop_is_blocked` blocks the loop with a
  synchronous `time.sleep` — the way a real policy does inside a tokenizer — and asserts beats still land in the
  database. An asyncio heartbeat records zero beats there. (Ruff's `ASYNC251` correctly objects to
  `time.sleep` in an async function; that one call carries a `noqa` with the reason, since blocking the loop is
  the scenario.)
- **A heartbeat that only stamps `heartbeat_at` is useless.** Each beat must also extend `lease_expires_at`, or
  the lease runs out under a worker that is demonstrably alive and the reaper takes its work.
- **Lease expiry means the worker is gone, never that the attempt took too long** (spec's own words), so
  `reap_expired_attempts` returns the obligation to `open` **without** charging an attempt. Charging would let a
  node that keeps dying quietly consume every obligation's budget — the same silent pass-rate depression
  `infra_error` exists to prevent. The reaper also expires the attempt and releases the obligation in one
  function: doing them separately leaves a window whose crash strands an obligation `in_progress` with no live
  attempt, which nothing would ever reclaim.
- **The reaper deliberately does *not* call `release_obligation`.** That function raises when the obligation is
  not releasable, which is right for one worker reporting its own outcome and wrong for a best-effort batch
  sweep that must not abort on one odd row.
- **`SKIP LOCKED` needs a genuinely concurrent test to mean anything.** Two sequential claims pass against a
  completely unlocked implementation; `test_two_concurrent_claims_never_take_the_same_obligation` runs eight
  claims over eight connections through `asyncio.gather` against four obligations and asserts the claimed ids
  are distinct — which fails under either "handed out twice" or "serialized and blocked".

## Implementation notes: control loop facts (M2.4)

These surfaced while building `lean_agent_core.worker`, the loop that joins M2.2's state machine to M2.3's
scheduler, against a real PostgreSQL 16 as the real `app` role.

- **Known gap, planned rather than guessed: reassembly scheduling (spec §6.4). Scheduled for Phase 4.**
  Two of §6.4's arrows are unreachable, and they are halves of one problem rather than two problems:
  `claim_attempt` selects `status = 'open'`, so a `decomposed` parent is never claimed and can never present the
  attempt spec requires ("Reassembly produces a real attempt with a real link and flows through `mark_proved`
  like anything else"); and nothing calls `mark_blocked`, so a child reaching a terminal status never causes its
  parents to be re-evaluated and `groups_exhausted` is dead code.

  **Both are pinned by `xfail(strict=True)` tests in `tests/db/test_state.py`** ("Known gap: reassembly
  scheduling"), which report as expected failures today and fail loudly the moment either is closed — verified by
  simulating the fix and confirming the `XPASS(strict)` failure. Whoever closes the gap removes the markers.

  **Why Phase 4 and not sooner**: nothing in `packages/` inserts an `obligation_edge` (only tests do, by hand), so
  no component can produce a `decomposed` obligation at all. The only policy that decomposes is
  `DecomposeAndConquer`, which spec §8 places in **Phase 4** — whose exit criterion, "a multi-`sorry` file closed
  end to end with a materialized artifact that links", *is* the validation this design needs. Phase 3
  (`WholeProofSampler`, `RepairLoop`) decomposes nothing, so the gap stays unreachable through it.

  **Four questions Phase 4 has to answer**: who re-schedules a `decomposed` parent; what a *failed* reassembly
  returns it to; who calls `mark_blocked`; and whether a reassembly attempt charges `budget_attempts` (spec's
  diagram is silent).

  **Provisional recommendation** — promotion, not widening the claim. A `SECURITY DEFINER`
  `promote_reassembly(parent)` moving `decomposed -> open` only when some group is fully proved, called from the
  control loop's commit path when a child reaches a terminal status — the same hook that would call
  `mark_blocked` when every group is dead. Three reasons it beats widening `claim_attempt`'s `WHERE`: it leaves
  the claim untouched, which protects spec's Phase 3 exit criterion that "the Phase 2 symbolic baseline still
  passes bit-identically" (widening changes the candidate set, and `ORDER BY priority DESC, depth ASC` would sort
  shallow parents ahead of existing work); it needs **no new status and no migration**, since a promoted parent
  is `open` with edges and a failed reassembly's `release_obligation` returns it to `open` to retry until budget
  exhausts — which dissolves the "lost `decomposed` marker" worry, because the runner reads group state rather
  than the parent's status; and it makes "who noticed the group completed" explicit instead of hiding it in a
  `WHERE` clause. Genuinely open within that: whether the trigger is the commit path (timely, couples the loop to
  the DAG) or a sweeper beside the reaper (simpler, matches precedent, adds latency).
- **Only a *declared* `InfraError` is unbudgeted; an undeclared exception is charged as a failed attempt.** This
  is a deliberate refinement of §6.4, not an oversight. Treating every crash as infrastructure means an
  obligation that reliably crashes the policy is retried forever at no cost, occupying a worker indefinitely and
  never reaching `failed`. A policy that consistently blows up on one obligation is telling you something about
  that obligation, and the attempt budget is the mechanism that eventually stops asking. The `infra_error` path
  keeps spec's meaning for the case spec actually names — a crashed Lean worker, an unreachable endpoint.
- **"Budget exhausted → failed" has to be applied at commit, not left for the next claim.** `claim_attempt`
  filters out obligations whose budget is spent, so one parked in `open` with nothing left is never picked up
  again to *notice* it is done — it sits there looking schedulable forever. The loop charges, then re-reads, then
  transitions, which is also spec's "checked coarsely [at claim] and exactly at commit".
- **A lost lease is not uniformly fatal to an attempt's result, and splitting by outcome is the right call.**
  `PROVED` and `DECOMPOSED` are still committed: a verified proof is verified regardless of who holds a lease,
  `mark_proved` independently re-checks the §1.1 predicate against the verdict, and a decomposition's children
  are already in the database. Discarding real, independently-validated work over a scheduling timeout is pure
  loss. Everything else is dropped, because the reaper already returned the obligation to `open` and another
  worker may hold it — charging it would take budget from work that is no longer ours.
- **A late-finishing loop must not overwrite the reaper's account of an attempt.** `_finish_attempt` only sets
  the attempt's status while it is still `claimed`/`running`; one the reaper marked `expired` stays `expired`,
  and `finished_at` is `COALESCE`d rather than reset. Stamping `failed` over `expired` would erase the only
  evidence that a worker was flapping. Spend is recorded either way — tokens and kernel time were genuinely
  consumed whoever ended up owning the attempt.
- **Shutdown is checked between attempts, never during one**, and the idle wait is an interruptible
  `asyncio.wait_for(shutdown.wait(), timeout=backoff)` rather than a `sleep`. Interrupting a claimed attempt
  would leave it to be reaped, discarding real work to save at most one lease; sleeping through a full backoff
  interval would make a draining worker look hung.
- **The runner is injected, not a `Policy`.** Spec §6.4 looks up a policy by `attempt.policy_id` and Appendix A
  defines `Policy.propose()` yielding `Action`s an executor performs — both M2.5. Defining a placeholder
  `Policy` protocol here would only be something M2.5 replaces, so the loop takes
  `AttemptRunner = Callable[[ClaimedAttempt], Awaitable[AttemptResult]]` and M2.5 supplies the real one.
  `AttemptResult.outcome` is an `ObligationOutcome`, so a runner cannot invent a transition the diagram has no
  edge for; `BUDGET_EXHAUSTED`/`GROUPS_EXHAUSTED` are rejected outright, since a runner has no way to know
  whether the budget is spent or every competing group is dead.

## Implementation notes: policy/executor facts (M2.5)

These surfaced while building `lean_agent_core.{actions,protocols,executor}` and
`lean_agent_policies.symbolic`, against the real v4.33.1 toolchain and a real PostgreSQL.

- **A sealed goal is a `def`, so a tactic facing it sees an opaque constant — the development must
  `unfold` it first.** Against the real kernel: `decide` reports "failed to synthesize Decidable
  Goals.G_arith", `simp` "made no progress", `omega` "no usable constraints". `unfold <goal>; <tactic>` fixes all
  three, and `delta` works too. `with_unfolding_all` does **not** — it changes what defeq checks may unfold, not
  the goal's syntactic form. Unfolding is not the agent stating the goal: it names a constant and asks Lean to
  expand it, and the kernel still type-checks the result against the sealed constant at link time.
- **The proof form is `def sol : LeanAgent.Goals.G_x := by ...`** — the entry's *type is the sealed constant*,
  so spec §1.1's "the agent never writes the goal statement" holds of the text a policy emits, not merely of
  what the kernel checks afterwards. Universe parameters must be spelled out on both the entry and the goal
  reference. A `def` whose type is a `Prop` draws `linter.defProp` on every Prop goal, which is pure noise in a
  successful attempt's diagnostics; it is silenced in the generated source, and that is safe because linters
  cannot affect link, replay or audit (unlike spec §7.2's option deny-list, which is about options that change
  what is *checked*).
- **`verdict.attempt_id` is a primary key, so one attempt gets one verdict — which forces "screen with `check`,
  spend the one `link`".** Linking every portfolio member blew up on the second tactic with a `verdict_pkey`
  violation. That is the schema saying what spec's endpoint table already said in one line each: `/v1/check`
  "elaborate a body against a base env", `/v1/link` "then replay and audit; **writes the verdict row**". The
  executor now screens each candidate through `/v1/check` (cheap, cached, writes nothing) and commits only a
  candidate that elaborates to `/v1/link`, once — after which the attempt is over regardless of outcome, because
  its one verdict is spent. A retry is a *new attempt*, which is exactly what the primary key is telling you.
  A candidate that passes `check` and fails `link` is information rather than noise: `check` only elaborates,
  while `link` re-checks in the kernel with forced options, replays and audits.
- **`/v1/check` grew a `bundle_sha`, and it belongs in the cache key.** A screening development names the sealed
  goal constant, so its worker needs the bundle on its path — the same one-warm-slot-per-(base env, bundle) cost
  `/v1/link` pays. Putting `bundle_sha` into `compute_cache_key`'s options is not bookkeeping: the same body
  against two different bundles is two different checks, since the constant it names means different things, and
  a key that ignored it would serve one bundle's answer to another's question — a wrong *acceptance*, not a
  stale one.
- **A `Protocol` with bare attribute annotations demands *settable* attributes, which a frozen dataclass is
  not.** mypy caught `SymbolicPortfolio` failing `Policy` for exactly this. Declaring each member as a
  `@property` accepts both, and is the honest requirement anyway: a policy whose `config_hash` could be
  reassigned after the run manifest recorded it is unreproducible in precisely the way §7.3 exists to prevent.
  Worth a `def _conforms_to_policy_protocol() -> Policy: return SymbolicPortfolio()` in the package itself —
  without a *typed* use inside `packages/`, protocol drift only surfaces at the executor's first call, since
  `mypy --strict` runs over `packages` and not `tests`.
- **§7.1's provenance rule is one-directional and implemented as such.** Nonzero completions means *not*
  `symbolic` and the provenance must come from the backend that served them; zero completions is `symbolic` even
  when a backend was registered, because a policy that could have asked a model and did not produced a symbolic
  trajectory. Completions with nothing to attribute them to *raise* rather than fall back — `trajectory.provenance`
  is `NOT NULL` with no default precisely so an unattributable trajectory cannot reach the corpus.
- **`PolicyContractError` is deliberately not an `InfraError`.** A policy proposing a tool outside its own
  allowlist, or an action no executor implements, is a bug in the policy, not the infrastructure — and M2.4's
  finding applies again: treating it as unbudgeted would let a misconfigured policy retry forever at no cost.
- **The `LeanService` implementation used by the end-to-end test lives in the test.** The protocol needed one
  real implementation to show its shape is right, and the *deployed* client is an httpx one that belongs with
  M2.9. Writing it over the real FastAPI app exercises the genuine `/v1/link` path — real worker, real kernel —
  without committing early to an HTTP client design.

## Implementation notes: ingestion facts (M2.6)

These surfaced while building `lean_agent_api.ingestion` against a real kernel and a real PostgreSQL.

- **Spec's `obligation` DDL contradicts spec §4.1, and the migration resolves it toward §4.1.**
  `sealed_olean_sha bytea NOT NULL` cannot be satisfied at obligation-creation time, because §4.1 also says "the
  `.olean` artifact is produced lazily out of band ... the hot path never waits on the build system". It is now
  nullable, meaning "not yet materialized" — and that is *self-enforcing* rather than merely documented, since
  `mark_proved` compares `v.sealed_olean_sha_observed = o.sealed_olean_sha`, which is NULL (never true) while
  this is NULL. An obligation whose bundle was never built cannot reach `proved`, which is exactly right.
- **`obligation.bundle_sha` is new, and it is not the same digest as `sealed_olean_sha`.** §4.1 names the
  generated file `LeanAgent/Goals/Bundle_<digest>.lean` where the digest is of the bundle *source* — that is
  what tells a worker which module to import. The compiled artifact's digest cannot serve, since you would have
  to build the file to learn its name. Two digests, easy to conflate, and M2.1.2's notes say the same thing from
  the other end.
- **`app` has no `INSERT` on `run` in spec §5.5's grant list, which is an omission.** §6.1 puts `POST /v1/runs`
  in the *public* API — the `app` role — so ingestion cannot create the run it is asked to create. Confirmed the
  hard way: "permission denied for table run". Granted `INSERT` only; §6.1's cancel endpoint will need
  `UPDATE (status)` and should get it then, with its own reason, since `run.status` gates `claim_attempt`.
- **`/v1/decompose` elaborates with `autoImplicit` at Lean's default (*on*), unlike `/v1/seal`, which forces it
  off — so a submitted file containing a typo does not fail.** The typo is auto-bound as a binder, decomposition
  reports success, and the abstracted statement carries it as an honest explicit `∀`. Found by a test that
  expected a file containing `NoSuchIdentifier` not to elaborate and got four obligations. This is the design
  working as intended, not a bug: §4.5 lists "elaborates only with `autoImplicit true`" as a *signal*,
  non-blocking, "recorded on the obligation and reported" — forcing it off during decomposition would reject a
  class of submission spec deliberately admits. The consequence for implementation is that the signal must be
  measured **on the submitted source**, before decomposition erases the evidence: once the statement is
  abstracted the generalization is written down and there is nothing left to detect.
- **Ingestion creates roots and no edges.** §6.3 step 4 mentions "edges recording reassembly", but a submitted
  file has no parent obligation to hang them from — `root_obligations` is a list and each `sorry` site is a root.
  Reassembling a *file* is materialization (step 6): the assembled file elaborates and each declaration links.
  Edges appear when a *policy* decomposes an obligation, which is a `Decompose` action no executor performs yet,
  so M2.4's "nothing re-claims a `decomposed` parent" seam is still untouched — M2.6 does not force it either,
  contrary to what looked likely from M2.5.
- **A decomposed lemma whose statement does not round-trip is refused rather than sealed.** M2.1.3's check says
  the printed text does not seal back to the `Expr` it came from, so an obligation created from it would prove
  something other than the file needs — statement drift arriving through the one door that bypasses sealing's own
  guarantee.
- **Two of §4.5's five signals carry a `[measure]` marker, so admission records *what happened* rather than a
  judgement.** `closed_by`/`closed_in_ms` say which tactic closed a root and how fast; whether that is "too
  fast" is a threshold nobody has measured, and inventing one would bake an unmeasured number into the schema.
  Two other signals are absent for concrete reasons: free universe metavariables surface as an ordinary
  elaboration error (M1.1) and are already in a seal failure's diagnostics, and "no binder is used in the body"
  needs `Expr`-level analysis nothing exposes yet.
- **`exact?` closes more than you would guess when picking a "hard" test goal.** `∀ n m : Nat, n + m = m + n`
  looked like ordinary work and is closed by `exact?` in ~230 ms via `Nat.add_comm` — which is the admission
  signal firing correctly. A goal that genuinely needs induction (`∀ n : Nat, 2 ^ n ≥ n + 1`) is what tests
  "the signal stays quiet on ordinary work".

## Implementation notes: materialization facts (M2.7)

- **Materialization is not an optimization; it is what makes an obligation provable at all.** Ingestion leaves
  `sealed_olean_sha` NULL, and `mark_proved` compares the observed digest against it — NULL is never equal, so
  before the bundle is compiled *no* obligation can reach `proved` however correct its proof. The end-to-end test
  keeps the counterfactual (`materialize=False`) alongside the real path precisely so that stays visible.
- **`sealed_olean_sha` must not be `app`-writable, so materialization goes through a `SECURITY DEFINER`
  function.** An `app` that could write the column could set it to whatever digest a verdict happened to observe,
  which turns spec's seal-integrity check into a tautology and defeats the one thing it catches — a worker
  importing a different bundle than the obligation was created against. `materialize_bundle` is **write-once**
  (fills NULL only), which extends that from "true at the first write" to "true for the obligation's whole life",
  and it *raises* if asked to stamp an already-materialized bundle with a different digest, since two
  compilations of one content-addressed source disagreeing means the build is not reproducible or the bundle root
  was tampered with.
- **A caller can still pass a digest it did not compute, and that is safe in the only direction that matters.**
  leanserv observes the *real* digest of the file it actually imported (M2.1.2), so a lie makes `mark_proved`
  refuse every subsequent proof rather than accept a bad one. Lying costs you your own proofs.
- **`store_or_inline` is the wrong call when something must be retrievable later.** M2.6 stored the bundle source
  with it, which deliberately stores *nothing* under 64 KiB — it decides how a value is carried in a column, a
  different question — so every small bundle simply vanished and materialization had nothing to compile.
  `blobs.put` is the right one, and its returned digest is sha256 of the content, i.e. exactly `bundle_sha`.
- **`/v1/link` now records the accepted development as `verdict.proof_blob`.** That is the only place the proof
  text can live without widening `app`'s grants: `obligation.proof_blob` is not in app's permitted-column list,
  deliberately, and `verdict` is the row leanserv already writes.
- **Spec §6.3 step 6's reassembly text belongs on the *run*, not on an edge — and framing that as "part of the
  decomposition-edge decision" was wrong.** Spec has a `reassembly_blob` on `obligation_edge`, "on the group,
  not the child", and that is a *decomposition group*'s reassembly, produced when a policy decomposes an
  obligation. A submitted file's reassembly is a different artifact: it exists before any policy runs, has no
  parent obligation to hang off, and covers every root at once. Once named that way the answer is obvious, and
  two milestones of deferring it were deferring the wrong question.
- **The assembled file inlines the sealed bundle rather than importing it.** Importing `Bundle_<sha>` would be
  simpler and would make the artifact useless: a `.lean` file a person takes away must not depend on a per-run
  generated module they do not have. Inlining costs nothing, since the bundle source is content-addressed and
  already stored, and the test compiles the emitted artifact with `lake env lean` against nothing but the
  toolchain — no bundle root on `LEAN_PATH` — which is the only real check on "standalone".
- **§6.3 step 6's second check is read from the recorded verdicts, not re-derived, because re-deriving it would
  corrupt the attempt record.** `/v1/link` writes a `verdict`, whose `attempt_id` is a foreign key to a real
  `attempt` — so re-linking at materialization means inventing attempt rows no policy ran, which the scheduler,
  the budget accounting and every pass-rate calculation would then see. What is verified instead is the §1.1
  predicate over each hole's accepted verdict, including `sealed_olean_sha_observed = obligation.sealed_olean_sha`;
  what ties that to the file in hand is content-addressing, since the inlined goals are the bundle's own text
  fetched by `bundle_sha` and cannot have drifted. Spec's worry ("the materialized file could compile cleanly
  with a drifted statement") is closed by the digest rather than by a second link.
- **`/v1/check` takes a *body*, so an assembled file must be split before it is checked.** A body carrying its
  own `import` line fails with "invalid 'import' command, it must be used in the beginning of the file" — the
  warm worker has already imported the base env. The renderer returns `(header, body)` and the artifact is their
  concatenation, so what is checked is exactly what is emitted minus a header that adds nothing to check.
  (Comments *are* allowed before `import` in a real file — verified — so the artifact's header is well-formed.)
- **The run row is written once and never updated, which is why decomposition happens before the INSERT.**
  `app` holds `INSERT` on `run` and deliberately not `UPDATE` (M2.6), and rather than widen that grant to store
  the reassembly, ingestion computes it first and puts it in the same INSERT. That is also the more honest shape:
  the reassembly is part of the submission's frozen record, exactly like the manifest beside it.
- **A test of a *global* sweeper must not assert a global count.** `test_reaper_*` asserted
  `reap_expired_attempts(...) == 1`, which silently coupled them to the whole database being otherwise idle;
  they broke the moment another suite left an expired lease around. They now assert on the specific attempt's
  status, which is the property actually under test.
- **`/v1/base-env/materialize` (§6.2) is also absent.** It builds and snapshots a *base environment*, which is a
  different artifact from a goal bundle, and spec §8 already defers the snapshot machinery it would need ("L3
  snapshot persistence: warm sealing needs a warm worker, not a persisted snapshot").

## Implementation notes: public API facts (M2.8)

- **Spec §5.5's grant list keeps turning out to be incomplete for §6.1's own endpoints.** `run`
  (`POST /v1/runs`, M2.6), `run.status` (`POST /v1/runs/{id}/cancel`) and `base_env`
  (`POST /v1/base-envs`) are all public endpoints whose tables `app` had no write on. Each was found
  by a test hitting "permission denied", never by reading the list. Every grant added is the
  narrowest that works -- `INSERT` only on `run` and `base_env`, column-level `UPDATE (status)` on
  `run` -- because the alternative is exactly what §5.5 warns against: the submission's frozen
  record (`manifest`, `axiom_allowlist`, `allow_sorry`, `reassembly_blob`) must not be editable
  after the fact, or a published result means nothing.
- **Cancel is `UPDATE run SET status`, and that *is* the whole mechanism.** `claim_attempt`
  requires `r.status = 'running'`, so setting the column stops every future claim; live attempts are
  deliberately left alone ("live attempts finish or expire"), since killing one discards work that
  may be seconds from a verdict and the lease already bounds how long a cancelled run holds a
  worker. The test asserts the *scheduling* consequence -- a later `claim_attempt` returns `None` --
  not just the column.
- **`:name::type` does not work in a SQLAlchemy `text()` query.** `SELECT :root::uuid` is parsed as
  the parameter `:root` followed by the parameter `:uuid`, and asyncpg reports `syntax error at or
  near ":"`. `CAST(:root AS uuid)` says the same thing without the collision. Worth knowing before
  writing any recursive CTE that seeds from a bound id.
- **Obligation listing is keyset-paginated on `id`, not `OFFSET`.** A run's obligations are being
  inserted and updated while a client pages through them, and `OFFSET` over a moving set silently
  skips and repeats rows.
- **`/v1/runs/{id}/events` polls rather than using `LISTEN`/`NOTIFY`.** Push would mean a dedicated
  backend connection per subscriber held open for the life of a run -- the same objection spec
  raises against session-level advisory locks for leases ("pin one backend connection per in-flight
  attempt ... to buy seconds of detection latency"). The stream emits a `snapshot` first so a
  subscriber joining mid-run is not left guessing, then one `transition` per obligation whose status
  actually changed, and ends when the run does.
- **`/metrics` is hand-written Prometheus text exposition, no client library.** Every number is a
  count of rows, so `prometheus_client` would add a dependency and a registry to format eight lines.
  The test creates a run first and asserts on a *series* (`lean_agent_runs{status="running"}`),
  because asserting only on the `# TYPE` header would pass against an endpoint emitting headers and
  no data.
- **`/healthz` touches nothing but the process; `/readyz` touches the database.** A liveness probe
  that fails when Postgres is briefly unreachable gets the process killed for someone else's outage.
- **`/v1/blobs/{sha}` refuses tenant-scoped blobs rather than serving them.** Spec marks the
  endpoint tenant-scoped and `blob.tenant_id` carries the scope, but the check needs an
  authenticated caller and multi-tenancy is post-MVP. Serving a tenant-owned blob from an endpoint
  that cannot tell who is asking would be the wrong way to round that gap, so only shared
  (`tenant_id IS NULL`) blobs are served and the rest are a 403 that says why.
- **`POST /v1/runs` materializes synchronously and refuses outright without a materializer.** §4.1
  keeps the build off the *hot path* (a check), not off submission, and a caller handed a 201 with
  obligation ids should be able to act on them. A deployment with no materializer would accept work
  that can never complete (M2.7), so it returns 503 instead of half-working.
- **`base_env` registration is content-addressed over the recipe, and `curated` cannot be set by a
  caller.** A duplicate digest would mean a second full warm-worker memory slot for an identical
  environment (§6.2's header fragmentation), and a caller able to mark its own prelude curated could
  opt itself into the hot pool.
- **The OpenAPI document is asserted to contain every §6.1 path.** Spec says it is "generated, not
  written", and that test is the cheapest check that no endpoint was quietly dropped.

## Implementation notes: miniF2F exit-gate facts (M2.10)

These surfaced while building Phase 2's exit gate -- `packages/eval/src/lean_agent_eval/suites/`
(`minif2f.py`, `vendor_minif2f.py`, `survey_minif2f.py`) and `tests/eval/test_minif2f.py` --
against real miniF2F statements, a real full-Mathlib base env and a real PostgreSQL. It is the
first milestone whose inputs are *real mathematics* rather than `Nat` toys, and that alone found
four bugs that nine milestones of testing had not.

- **`checkSealed` required an *empty* axiom cone, which silently excluded classical mathematics.**
  A goal's cone includes the axioms behind the definitions its *type mentions*, so anything built
  on `Real` carries `Classical.choice` and `Quot.sound`, and even `91 ^ 2 = 8281` carries `propext`
  through its `Monoid` instance. Measured: 8 of 13 easy-tail statements failed to seal, **with
  empty diagnostics**, because the rejection branch had nothing to report. It went unnoticed since
  M1.1 because every earlier test sealed bare `Nat` statements, whose cones happen to be empty.
  The check now rejects `sorryAx` specifically -- which is what M1.1's own rationale actually
  described ("a `sorry` in the statement itself elaborates with only a warning") -- and says so in
  `diagnostics`. What the statement's definitions rest on is the base environment's trust question
  (spec §4.3), not the goal's; the *proof*'s axioms are audited separately at link time.
  Generalizes: **a rejection that returns no diagnostic is a bug even when the verdict is right**,
  and "no axioms at all" is almost never the property you want from something that mentions
  real mathematics.
- **The executor screened proof candidates on `ok` alone, so a `sorry` spent the attempt's one
  verdict.** `verdict.attempt_id` is a primary key, so the executor screens with `/v1/check` and
  commits one `/v1/link`. But a `sorry` is a *warning*: `apply?`, `exact?` and `rw?` routinely
  report "found a partial proof", emit their suggestions, and let Lean's error recovery fill the
  hole with `sorryAx` -- all with `ok=true`. So the first suggestion tactic in the portfolio
  consumed the attempt, the audit rejected it, and **every tactic behind it was unreachable**;
  with `DEFAULT_TACTICS` ordering `exact?`/`apply?`/`rw?` ahead of `linarith`/`nlinarith`/`aesop`,
  that is most of the portfolio on most goals. `/v1/check` now reports the new declarations'
  transitive axiom cone (populating spec's own `verification_cache.axioms`, so it survives a cache
  hit -- otherwise a screen would reject on a cold run and accept on a warm one), and the executor
  skips a candidate depending on `sorryAx`. Gated on the run's `allow_sorry` so the screen says
  exactly what the audit will say. `tests/test_executor_screen.py` pins it with no infrastructure;
  both bug-specific tests were confirmed to fail with the fix disabled.
- **`SymbolicPortfolio` needed `intros`, and the reason is this system's own sealing.** Sealing
  turns a submitted `theorem f (x : T) (h : P) : C` into the closed statement `∀ (x : T), P → C` --
  the binders that were in the *signature* become part of the goal -- so a tactic that would have
  faced `C` with `x` and `h` in context now faces a `∀`, which `omega`/`linarith`/`rfl` simply fail
  on. Without `intros` the portfolio closes only hypothesis-free problems. It is a no-op on a goal
  with no binders, so it cannot cost a proof.
- **Do not `set_option` a Mathlib linter from code that must run on any base env.** Silencing
  `linter.unusedTactic` (which `intros` trips on a hypothesis-free goal) broke *every* `Init`-only
  test at once: that linter ships with Mathlib, not core, and `set_option` on an unknown option is
  a hard **error**, not a warning. An unknown *tactic* degrades to one failed portfolio member; an
  unknown *option* fails the whole development. `linter.defProp` is core and stays.
- **`ReplWorker` had a 64 KiB response ceiling and no taxonomy for hitting it.** `asyncio`'s
  `StreamReader` defaults to a 64 KiB line limit, and a response is one JSON object on one line;
  exceeding it raises a bare `ValueError("Separator is not found...")` from inside `readline`,
  outside the whole `ReplCrashed` taxonomy. Real traffic hits this: the largest response measured
  across all 488 problems was **217 KB** (a `check` whose diagnostics carry a large real goal
  state), while the largest `decompose` was only 1,964 bytes. The limit is now 32 MiB -- raised,
  not removed, since an unbounded reader turns a runaway `#eval` print loop into an unbounded
  allocation -- and an over-long line is a `ReplProtocolError` that kills the worker, because
  `readline` has consumed an unknown amount of it and the stream can no longer be resynchronized.
- **A survey that rehearses an approximation of the real thing measures the wrong number.** The
  first easy-tail survey substituted tactics into upstream's own theorem and reported 126/488. That
  shape does not survive sealing (see `intros` above), and its win condition read `ok` alone, so it
  counted every partial `apply?` as a proof. Rebuilt to run `SymbolicPortfolio.development()`
  verbatim against the statement `/v1/decompose` actually produces, and to judge on the axiom cone:
  **114 of 488 (23.4%)**, plus 57 that decompose but whose printed statement does not round-trip,
  so ingestion refuses to seal them (M2.6). Same lesson as M2.1.3's round-trip check.
- **The toolchain gap costs nothing at elaboration.** Upstream targets v4.24.0 and this repo pins
  v4.33.1, and **all 488 statements still elaborate** -- zero decomposition failures. The 57 losses
  are entirely a pretty-printing round-trip problem (coercion arrows, mostly), not a Mathlib API
  drift. Worth knowing before assuming a corpus/toolchain mismatch is fatal, which is the
  assumption that has had Phase 1 gate 1 blocked since planning.
- **A full `import Mathlib` warm worker measures ~6 GiB RSS and ~30 s to warm**, against `Mathlib.
  Algebra.Group.Basic`'s ~1.5 GiB (gate 9). Two consequences the gate is built around: `/v1/link`
  keys its worker on `(base_env, bundle)` while sealing keys on the base env alone, so an uncapped
  pool holds **two** such workers (~12 GiB) on a 16 GiB runner -- the suite caps
  `max_total_workers=1` and orders its phases (ingest, materialize, link) so exactly one eviction
  happens. And N separate runs would mean N bundles, N pool keys and N warm-ups, which is why the
  gate submits its whole tail as **one multi-`sorry` submission** -- also the more faithful reading
  of spec's "*a* materialized file passes both the elaboration and the link check".
- **Compiling a Mathlib-importing bundle with `lake env lean` costs only ~5.5 s**, far less than
  the ~30 s a warm worker needs for the same imports: `.olean` loading is mmap-backed, while a
  warm worker additionally populates elaborator extensions (`loadExts`). Do not size one from the
  other.
- **The gate's expectations are checked in, not recomputed.** `EASY_TAIL` is a fixed list of ids;
  a gate that worked out for itself which problems ought to be easy could not fail, since a
  regression would just redefine the tail and stay green. `MEASURED_TAIL` keeps the full survey
  result beside it for comparison without asserting it.
- **The corpus is vendored and digest-pinned, never fetched.** Spec §7.5 wants the benchmark held
  read-only "so proving a weakened restatement is structurally impossible"; a suite that runs "on
  every PR forever" must not depend on a CDN; and an upstream force-push would otherwise silently
  change what is being measured. `vendor_minif2f.py` rebuilds it by hand from a pinned commit,
  verifies every assumption it rests on (488 problems, one theorem each, identical headers), and
  records the upstream toolchain. MIT, Copyright (c) Meta Platforms.
- **`lean-action` runs `lake exe cache get`**, downloading all ~8,690 Mathlib `.olean` files, so
  CI genuinely has a full Mathlib to import even though nothing in this package imports it at
  compile time. The gate's own fixture still checks for `Mathlib.olean` and turns a missing build
  into a hard failure under `LEANKERNEL_REQUIRED=1`, for M2.1.1's reason.
- **A blunt `"sorry" not in artifact.source` is the wrong assertion.** Decomposition names each
  hole `sorry_<n>`, so a correct artifact legitimately contains `sorry_1` in a comment and
  `@sorry_13` in its reassembly term. The check needs a negative lookahead -- and it is still worth
  making, because a `sorry`'d file *elaborates* (warning, not error), so `elaborates` alone cannot
  rule one out.
- **Three separate gate runs must share one `asyncio.run`.** M1.8.4 recorded that an asyncpg
  engine must be created and disposed inside one loop; the three-run stability check is the same
  trap one level out, because `httpx.ASGITransport` dispatches into the leanserv app on the
  *calling* loop rather than on `TestClient`'s portal, so leanserv's engine is bound there too.
  Sharing a loop is not sharing state: each repetition is still its own run, obligations, attempts
  and verdicts.

## Implementation notes: CLI and client facts (M2.9)

- **`argparse` puts a global flag *before* the subcommand, and nobody types it that way.**
  `lean-agent run --json ...` failed with "unrecognized arguments: --json"; only
  `lean-agent --json run ...` worked. Both positions are now accepted by declaring the shared flags
  on a `parents=[common]` parser as well as at top level — and `default=argparse.SUPPRESS` on the
  subparser copies is what makes that *safe*: without it the subparser's own default overwrites a
  value given before the subcommand, silently turning `--json run` back off. Found by a test that
  wrote the flag the natural way.
- **The CLI is only an httpx client of §6.1, and that is the point of the milestone.** No database
  handle, no Lean toolchain, no shared code path with the server: if the CLI can do it, so can
  anyone else's client, which is the real test of whether §6.1 is a complete API rather than a
  convenient subset of one. Its tests drive `main(argv, client=...)` — the real parsing, the real
  command functions, the real client — against a `TestClient` wired to the genuine app.
- **`TestClient` is an `httpx.Client` subclass**, so it can be handed straight to a sync client
  under test. For an *async* client, `httpx.ASGITransport` routes into the app with no socket — but
  it does **not** run lifespan, so anything relying on startup/shutdown (leanserv's pool close)
  still needs a `TestClient` alongside it driving the same app object.
- **Exit codes are interface.** `0` did what was asked, `1` the request succeeded but the answer is
  "no" (incomplete run, unfilled holes, nothing sealed), `2` the request itself failed. Separating
  1 from 2 is what lets a script tell "this file could not be proved" from "the system is broken" —
  the same distinction `infra_error` draws inside the pipeline.
- **`--json` prints the API's own object, unreshaped.** Reshaping would make the CLI a second,
  undocumented schema drifting from the OpenAPI document.
- **`LeanServiceClient` deliberately does not retry.** A `/v1/link` that timed out may still have
  written its verdict, and `verdict.attempt_id` is a primary key — a blind retry would either
  collide or spend a second attempt's kernel time against one attempt's budget. Retry policy
  belongs with the control loop, which knows whether a *new attempt* is the right answer.
- **A client never closes a transport it was given.** Once a deployment shares one connection pool
  across several clients, closing someone else's breaks every other user of it. Tested.
- **`batch`/`from-folder` keep going past a file that fails.** For a benchmark folder, inputs that
  do not elaborate are the normal case rather than the exception; a batch that stopped at its worst
  input would be hostage to it. Each result is reported and the exit code summarizes.
- **The integration suites still carry their own `LeanService` adapter.** `LeanServiceClient` now
  has its own test against the real app, so the shipped path is covered; consolidating the three
  integration files onto it needs each `asyncio.run` body wrapped in an extra `async with` to own
  the transport, and that refactor does not belong in the same change as the client itself.

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

- **`/v1/replay` and `/v1/base-env/materialize` are not built.** Standalone `/v1/replay` has no caller:
  `/v1/link` already replays as part of the acceptance path, which is what spec §4.3 actually asks for, and a
  route with nothing real underneath it is exactly the half-finished surface this project avoids.
  `LeanKernel.Serve`'s wire protocol understands `check` (M1.8.1), `seal` (M2.1.1), `link` (M2.1.2) and
  `decompose` (M2.1.3).
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

## Implementation notes: seal-on-the-wire facts (M2.1.1)

These surfaced while building `/v1/seal` — `Serve.lean`'s `seal` request kind, `ReplWorker.seal`,
`api.py`'s route, and `lean_agent_core/digests.py` — against the real v4.33.1 toolchain and a real Postgres.

- **`lean_exe leankernel` was not a `@[default_target]`, so CI never linked the binary, and *every* Lean-dependent
  Python test silently skipped there from M1.8.2 until this milestone.** `LeanKernel.Main` is inside the
  `LeanKernel` lib's own glob, so its `.olean` was built and `lake build` reported success (11 jobs) — but the
  linking step for `.lake/build/bin/leankernel` is a separate target, and CLAUDE.md's own "builds `LeanKernel` +
  the `leankernel` exe" claim was simply wrong. The tell was in plain sight for four milestones: CI's
  `uv run pytest tests/leanserv tests/eval` step completing in **2 seconds** (43 collected, ~30 skipped), which
  nobody had reason to read because the job was green. **Two fixes, both needed**: the lakefile now marks the exe
  a default target, and CI's `lean` job sets `LEANKERNEL_REQUIRED=1`, which turns `tests/conftest.py`'s
  `lake_project_dir` skip into a hard failure. Generalizes past this one bug: a fixture that skips on a missing
  prerequisite is honest locally and dangerous in CI, so any such fixture needs an environment-gated "here it is
  mandatory" mode — and a green job proves nothing about tests that never ran, so check step *durations*, not just
  conclusions, when a suite is supposed to be exercising real infrastructure.
- **`SealGoal` grew a `levelParams` field in M2.1.3.** M2.1.1 shipped without it and was complete against every
  goal that existed at the time (hand-written statements, monomorphic or with universes Lean could infer from a
  metavariable); the gap only appeared once decomposition started producing statements that name their universes.
  See M2.1.3's notes for why `autoImplicit false` makes that field mandatory rather than cosmetic.
- **A goal bundle is elaborated one goal at a time, not as a single unit, and this is forced by `checkSealed`'s
  own semantics.** `checkSealed` (M1.1) reads the *whole* message log's `hasErrors`, so one bad goal in a combined
  elaboration marks every sibling failed — which would break exactly the behaviour spec §6.1 asks for ("a
  submission with ten goals of which one does not elaborate creates nine obligations and reports the tenth").
  Goals never reference each other, so per-goal elaboration costs only extra passes over an already-warm
  environment and buys exact attribution.
- **Both `name` and `statement` are spliced into generated source, so both are injection sites — defended
  structurally, not textually.** After elaborating a goal, `sealGoal` compares the environment's new constants
  against the base and rejects anything not under the goal's own declaration name. Confirmed empirically that all
  three attacks this stops are real against v4.33.1: a statement closing the `def` and the namespace to declare
  `Evil` outside; one declaring `Helper` *inside* `LeanAgent.Goals` (why the check is against the goal's own name,
  not the namespace — a smuggled sibling could collide with a later goal); and a `name` carrying
  `G : Nat := 0\ndef Evil2`, which `isValidGoalName`'s character allowlist rejects before elaboration even starts.
- **The check must be a name *prefix*, not equality — Lean really does generate auxiliaries under the sealed
  name.** A statement containing a `match` produces a genuine `LeanAgent.Goals.G_match.match_1` (verified directly
  with `lake env lean` and `#check`), so an exact-match escape check would reject legitimate goals. `Name.isPrefixOf`
  is reflexive, so the prefix form covers the plain case too.
- **`bundleSource` contains only the goals that actually sealed.** The bundle is compiled lazily out of band with
  nothing re-verifying it at that point (spec §4.1: "the hot path never waits on the build system"), so returning
  the full requested set would hand that compile step the very source the seal just rejected — an injected
  declaration would end up in the compiled `.olean` backing its innocent siblings. `reports`/`goals` stay parallel
  to the request either way, so the caller can still report the failures. Every emitted bundle was checked by
  actually compiling it with `lake env lean`, including the ones assembled after dropping goals.
- **Duplicate goal names fail every goal using them, at seal time.** Each elaborates fine alone, but Lean refuses
  to redeclare a name (M1.2's finding), so the shared bundle would not compile — and there is no principled way to
  pick which duplicate is "the" goal. Catching it here keeps the failure attributable to a request, instead of
  surfacing in an out-of-band compile with nothing to attribute it to.
- **`obligation.goal_digest` deliberately excludes the declaration name.** `decl_name` is generated per
  obligation, so folding it in would make every goal's digest unique and silently disable spec §5.2's cycle guard
  ("a child's `goal_digest` may not equal any ancestor's") — the guard needs two spellings of the same goal to
  *collide*. It digests statement source rather than an elaborated canonical form, which makes it sound but
  incomplete for that guard (no false "same goal", but a cycle spelled two ways slips past to `run.max_depth`,
  spec's unconditional backstop). Tightening it needs Lean-side canonicalization to exist first.
- **`/v1/seal` is uncached, unlike `/v1/check`.** `verification_cache` is keyed by `verdict_kind` — it answers
  "did this declaration check", not "what does this goal elaborate to" — and a `CachedCheck` has nowhere to carry
  per-goal reports or bundle source. Sealing is also elaboration-only against an already-warm worker, i.e. the
  cheap side of the very measurement that motivates warm workers (spec §4.1: cold ~78% of pipeline time, warm
  under 1%), so there is little to win and a wrong-shaped cache entry to lose.
- **A crashed worker is a 503, not a `SealResponse` with `ok=False`.** `seal_failed` is a statement about the
  content ("the statement does not elaborate"); reporting an `infra_error` in its shape would tell the caller not
  to create obligations for goals that were never actually judged — the same distinction spec draws everywhere
  else, at the one place in this endpoint where it is easy to blur.
- **The wire protocol's `kind` field defaults to `"check"` when absent, and `ReplWorker.check` deliberately keeps
  sending no `kind` at all** — that keeps M1.8.1's exact request bytes a *tested* path rather than an asserted
  compatibility claim. `handleLine` now returns already-serialized `Json` rather than one response type, since
  `check` and `seal` answer with genuinely different payloads; they share `id`/`ok`/`diagnostics` so a caller can
  always read the outcome without knowing which kind it asked for.
- **`seal` is a reserved keyword in Lean v4.33.1** (the `seal`/`unseal` commands), so it cannot be bound as an
  identifier — a `let seal := ...` in a test fails to parse with a confusing "unexpected token 'seal'". Also:
  a structure instance broken across lines inside `#[{ ... }]` must have its continuation indented past the `{`'s
  own line start, or the fields after the first are rejected with "unexpected identifier; expected '}'".

## Implementation notes: link-on-the-wire facts (M2.1.2)

These surfaced while building `/v1/link` — `Serve.lean`'s `link` request kind, `ReplWorker.link`, `pool.py`'s
`bundle_root`, and `api.py`'s route and `verdict` write — against the real v4.33.1 toolchain and a real Postgres.

- **A sealed goal has to be a genuinely *compiled and imported* module before it can be linked against, so
  `/v1/link` needs a bundle on the worker's search path — which is why `PoolConfig.bundle_root` exists.** M1.1
  already recorded that `getModuleIdxFor?` returns `none` for anything defined in the module currently being
  elaborated, which means an in-session `seal` result (M2.1.1) can never satisfy Link's requirement. The bundle
  must first be compiled out of band, and `bundle_root` is the directory both that compile step (M2.7's
  materialization, not yet built) and `leanserv` agree on. `ReplWorker.spawn` prepends it to `LEAN_PATH`.
- **`lake exe` *merges* an inherited `LEAN_PATH` with the one it computes for the workspace rather than replacing
  it** — confirmed empirically before designing anything on top of it, because the whole approach dies if it
  doesn't hold: a worker started with `LEAN_PATH=<bundle root>` resolved both `Init` (from the toolchain, via
  Lake's own path) and a bundle module from an arbitrary directory in the same session.
  `tests/leanserv/test_repl.py` pins this directly rather than leaving it implicit under the HTTP tests, since it
  is a fact about someone else's tool that a Lake update could change.
- **`lake env lean` refuses an input file outside the package root unless `--root=<dir>` says otherwise**
  ("must be contained in root directory"). A generated bundle deliberately lives outside the Lake package (it is
  per-run, not a checked-in target), so compiling one is
  `lake env lean --root=<dir> <dir>/LeanAgent/Goals/Bundle_<sha>.lean -o <same>.olean`.
- **Two different digests are in play and they are easy to confuse.** `bundle_sha` (spec's `LinkRequest`) is the
  digest of the bundle *source*, and it names the file/module (`Bundle_<sha>.lean`) — that is what M2.1.1's
  `/v1/seal` returns as `bundle_digest`. `obligation.sealed_olean_sha` is the digest of the *compiled* `.olean`,
  and it is what `mark_proved` compares `verdict.sealed_olean_sha_observed` against. They are never equal; a test
  or caller that uses one where the other belongs fails in a way that looks like tampering.
- **`sealed_olean_sha_observed` is genuinely *observed*, not echoed.** `Serve.lean` reports which module
  `getModuleIdxFor?` resolved the goal to and that module's real path via `Lean.findOLean`; `api.py` hashes that
  file. Recording a digest the caller supplied would silently defeat spec's seal-integrity check, since
  `mark_proved`'s `v.sealed_olean_sha_observed = o.sealed_olean_sha` clause would then be comparing a value to
  itself. This works only because workers are local subprocesses today — a remote-worker deployment has to move
  the hashing into the worker, or the path is meaningless (or worse, names a different file) on the reading side.
- **Link's `expectedModuleIdx` is derived from the *base* environment, before the agent's development elaborates,
  never taken from the request.** That is what makes the shadow check meaningful here: the worker imported the
  bundle at startup, so the module the goal resolves to in the untouched base environment is by construction the
  module it was sealed into, and nothing the caller sends (or an agent could influence) participates in deciding
  it. What that check cannot catch — a deployment serving the *wrong* bundle at the right filename — is exactly
  what the observed-digest comparison catches, so the two compose rather than overlap.
- **`LinkReport` gained a `kernelOk` field separate from `ok`, because `verdict` has separate `link_ok` and
  `axiom_audit_ok` columns and M1.2 was collapsing both into one.** A `sorry`-backed proof is the case that makes
  this concrete and it is not a corner case: the term genuinely *does* have the sealed goal's type, so the kernel
  accepts it, and what rejects it is the run's axiom allowlist. Reporting that as `link_ok=false` describes it
  wrongly — the term is correct, the trust base is not — and any later analysis of failure modes would be reading
  a lie. Confirmed against the real toolchain in both directions (`sorryAx` denied and allowed).
- **`run.axiom_allowlist`/`run.allow_sorry` are read from Postgres by `/v1/link`, never accepted from the
  request.** Same boundary as "leanserv is the only writer of verdicts" (spec §5.5/§6.4): a caller that could
  pass its own allowlist could permit `sorryAx` for a run that forbids it. `allow_sorry` is folded in as the
  literal axiom name `sorryAx`, because that is exactly what the column means — `auditAxioms` deliberately applies
  no special case for `sorry`.
- **An unmaterialized bundle is a 404, checked before a worker is acquired — not the `infra_error` it naturally
  produces.** Left alone, a worker spawned for a nonexistent module dies inside `importModules`, and
  `ReplWorker` correctly classifies that as a crash; the response was `infra_error` with the diagnostic "stdout
  closed (process exited) while awaiting response". That taxonomy is wrong in a way that matters: `infra_error`
  is unbudgeted and invites retry, and no retry will materialize a bundle nobody built. It also burns a pool slot
  (and possibly an LRU eviction) to learn nothing. Found because the first version of the test asserted only
  `link_ok is False`, which passed for entirely the wrong reason — a reminder that a negative assertion should
  name *why* it failed, not just that it did.
- **A crashed worker's own message is about the pipe; what killed it is on stderr.** `_run_link` includes
  `ReplCrashed.stderr` in the verdict's diagnostics for exactly this reason — `ReplWorker` drains that pipe
  (M1.8.2) precisely so a Lean panic or import failure survives to be reported, and dropping it leaves an
  `infra_error` verdict that says nothing about what happened.
- **`paranoid` (spec's multi-kernel replay) is rejected with a 400 rather than accepted and ignored, and
  `kernels_agreeing` is always empty.** This distribution ships one kernel implementation, and spec §4.3 is
  explicit that independence across *implementations* is the only thing multi-kernel replay buys. Honouring the
  flag would mean replaying twice through the same kernel and reporting two agreeing "kernels" — corroboration
  that did not happen, recorded in the one column R18 says should key the reward signal.
- **Beware a "weakened mutant" that is merely defeq.** A first pass used `∀ n, n + 0 = n + 0` against a goal of
  `∀ n, n + 0 = n` and was briefly alarmed that it linked — but `n + 0` reduces to `n`, so the two types are
  definitionally equal and the kernel was right to accept it (this is spec §4.2's own "the kernel decides
  definitional equality" property working as designed). A genuine weakening needs a different proposition:
  `∃ n, n + 0 = n`, which M1.2's own gate-5 test already used.

## Implementation notes: decomposition-on-the-wire facts (M2.1.3)

These surfaced while building `/v1/decompose` — `Serve.lean`'s `decompose` kind, `Sorries.lean`'s warm entry
point, `ReplWorker.decompose`, and `api.py`'s route — against the real v4.33.1 toolchain.

- **A decomposed subgoal's statement crosses the process boundary as *text*, and text is not automatically a
  faithful stand-in for the `Expr` it was printed from.** `Decomposition.lemmas` is `Array (Name × Expr)` and
  `Expr` has no `ToJson`, so the wire has to carry pretty-printed source — which is also what the consumer wants,
  since the next thing that happens to a child is `/v1/seal`, which takes a statement as text. But an unfaithful
  print means a child proving something other than what its parent's reassembly needs, which is exactly the
  statement drift spec §1.1 designs away for *sealed* goals. Sealed goals get that guarantee structurally (the
  goal is elaborated once and frozen); a statement travelling as text has to *earn* it. So `Serve.lean` seals
  every printed statement on the spot and checks the result is defeq to the `Expr` it printed, reporting
  `roundTrips` per lemma. That check paid for itself on its first contact with real Mathlib goals — see below.
- **Rehearse the real thing, not an approximation of it.** The round-trip check's first version elaborated the
  printed statement as a bare *term*, and reported every universe-polymorphic Mathlib goal as broken:
  `∀ {G : Type u_1} [inst : Group G] ...` has `u_1` free, which in term position is an error that silently
  becomes `sorry`. That is not how the statement is ever consumed. Rebuilding the check to run `goalDeclSource`'s
  actual generated source — the same function `sealGoal` uses — got the right answer *and* found a genuine bug
  (next item). A test double of the thing under test is worth less than the thing itself when the thing is cheap
  to run.
- **`autoImplicit false` does not bind a free universe *name*, and `sealGoal` forces `autoImplicit false` — so
  M2.1.1 could not seal any decomposed subgoal that mentions a universe.** The error is "unknown universe level
  `u_1`". This does not contradict M1.1's "top-level `def` universe generalization is unconditional": that
  finding is about a free universe *metavariable*, which Lean generalizes regardless of options. A universe
  *name* is a different thing and needs the declaration to bind it. **Fix**: `SealGoal` grew `levelParams`, and
  `goalDeclSource` emits `def <name>.{u_1, v} : Sort _ := ...` — which is what spec §4.1's own bundle template
  showed all along (`def G_<id₁>.{u_0} : Sort _ := ...`) and M2.1.1 simply had not implemented, because nothing
  had yet produced a goal that needed it. Worth remembering as a shape: a milestone can look complete against
  every input that exists at the time and still be missing something its own spec text shows.
- **Level-parameter names are an injection site too.** They are spliced into `def <name>.{<here>}` exactly as
  `name` is, so they get the same character allowlist `isValidGoalName` applies — confirmed by sending
  `"v} : Sort _ := True\ndef Evil3"` as a level parameter and checking nothing named `Evil3` reaches the bundle.
- **`pp.fullNames` is forced when printing a subgoal statement.** The statement is consumed somewhere else
  entirely — a later `seal` request, with none of the originating development's `open`s or `variable`s in scope —
  so a name that only resolves inside that namespace scope would silently become a different constant, or fail to
  resolve, by the time it matters. Readability is the right thing to trade for context-independence here.
- **`decomposeWarm` exists because `Lean.Elab.process` discards infotrees.** `process` is exactly
  `IO.processCommands inputCtx {} (Command.mkState env {} opts)`; the only differences for decomposition are
  `infoState.enabled := true` (infotrees are what `extractSorries` walks, and building them is not free, which is
  why the other handlers leave them off) and returning the message log so a caller can tell "no sorries because
  the development is complete" from "no sorries because it did not elaborate". Everything genuinely delicate —
  `revert`'s own reverted-fvar list, `@`-application, hygiene-mangled instance binders, back-to-front splicing —
  lives in `decomposeElaborated`, shared with the cold `decompose`, so there is exactly one copy of it.
- **`/v1/decompose` needs no bundle on the worker's `LEAN_PATH`, unlike `/v1/link`.** Decomposition reads a
  development's own `sorry`s and never touches a sealed goal, so it runs on the same plain base-env worker
  `/v1/check` uses and shares its warm slots instead of fragmenting the pool further (spec §6.2's header
  fragmentation is the binding multi-tenancy constraint, so every avoided pool key matters).
- **A crashed worker is a 503; `ok=False` means "the development does not elaborate".** Same distinction
  `/v1/link` draws: `ok=False` is a statement about the content, and an infra failure is not evidence about
  content. `/v1/decompose` is also uncached, for `/v1/seal`'s reason — `verification_cache` answers "did this
  declaration check", and a `CachedCheck` has nowhere to carry per-lemma statements or a reassembly source.

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

## Implementation notes: gate 9 (prelude memory delta) facts

These surfaced while building and running `packages/leanserv/src/lean_agent_serv/memory_probe.py`, spec's exit
gate 9: "Prelude memory delta measured (R19 input)." R19: "Zygote fork may not be viable in Lean's threaded
runtime | The multi-tenancy capacity argument rests on it. **[measure]** in Phase 1. If it fails, deployment
shifts from shared nodes to per-tenant nodes." Gate 9's number is a direct input to that judgment call.

- **`ReplWorker.pid` (the `lake` process) is the wrong thing to measure memory of — the real cost lives in the
  `leankernel` child `lake exe`/`lake env` forks, confirmed the same way M1.8.2 found it for killing a worker.**
  `memory_probe.py`'s `_process_group_rss_kib` sums RSS for `lake`'s own pid *and* any process whose `ppid`
  matches it, via a plain `ps -A -o pid=,ppid=,rss=` listing filtered in Python — not `ps`'s own process-group
  selection flags, which differ in meaning between BSD `ps` (macOS) and Linux's procps, where a raw `pid,ppid,rss`
  column listing is consistently supported on both.
- **A single measurement of a warm ~1.5 GiB Mathlib worker's RSS varies by roughly ±1-2 MiB run to run, on its
  own, with no prelude involved at all** — confirmed empirically by measuring the same `Mathlib.Algebra.Group.
  Basic`-only base repeatedly: 1,567,968 / 1,569,744 / 1,570,112 KiB across three back-to-back spawns. This is
  ordinary allocator/GC-timing jitter in a long-running warm process, not a bug in the measurement code, but it
  has a real consequence: **a prelude smaller than a few thousand declarations is invisible against this noise
  floor.** Measuring 50-, 1,000-declaration synthetic preludes (`LeanKernelTests/SyntheticPrelude{50,1000}.lean`)
  produced deltas that varied in *sign* across repeats (from -2016 to +1952 KiB) — not a real per-declaration
  signal, just noise. Only at 10,000 declarations did the signal (mean +24,341 KiB, tight range +23,744 to
  +25,264 across 3 repeats) clear the noise floor by a comfortable margin.
- **Measured per-declaration cost, for the simplest possible declaration (a `decide`-provable `Nat` fact with no
  Mathlib content of its own): ~2.4 KiB/declaration** (24,341 KiB / 10,000, averaged over 3 paired repeats — see
  `SyntheticPrelude10000.lean`). This is a **floor, not a general estimate** — real project-local Lean libraries
  with actual mathematical content (larger terms, more complex types, deeper proof terms) would cost more per
  declaration than this synthetic minimum, which was deliberately built to isolate declaration-count cost from
  additional transitive-import cost (see the synthetic prelude files' own docstrings for why they need no
  Mathlib import of their own).
- **What this means for R19**: a prelude's own declaration content is cheap relative to the base import (~1.5
  GiB for `Mathlib.Algebra.Group.Basic` alone; spec's own worker memory cap is 12 GiB) — even a substantial
  project-local library (thousands of declarations) costs low-to-mid single-digit MiB, a small fraction of a
  worker's budget. The real capacity constraint spec's own architecture note already names ("a prelude-bearing
  worker occupies a full slot") is **slot multiplication**, not prelude content size: every distinct
  base-env-plus-prelude combination needs its *own* ~1.5+ GiB warm process regardless of how small that
  prelude's own content is, and that -- not per-declaration memory cost -- is what actually pressures the
  ~5-base-env/~21-worker capacity spec's header-fragmentation note describes. This doesn't resolve R19 on its
  own (whether the zygote fork's copy-on-write sharing is worth building is still a real design question), but it
  does narrow *why* it would matter: for slot count under fragmentation, not for prelude memory pressure per se.
- **`measure_prelude_deltas` pairs a fresh base measurement with each prelude measurement in the same round,
  rather than reusing one base measurement across an entire session.** Given the noise finding above, reusing a
  single base value across many later comparisons would conflate real signal with whatever the system's overall
  memory/scheduling state happened to be at one arbitrary earlier moment — pairing each round's own base against
  that round's own prelude measurement is what let the 10,000-declaration signal actually separate from noise
  with only 3 repeats.
- **`tests/leanserv/test_memory_probe.py` tests the tool's correctness, not any specific RSS number** — that a
  real spawned worker's process-group RSS is properly summed across `lake` and its `leankernel` child (confirmed
  by comparing against `lake`'s own pid alone, not merely asserting a positive number), that
  `measure_prelude_deltas` computes `delta_kib` correctly and respects `--repeats`. All five tests use `Init`-only
  imports to stay fast; they say nothing about the actual measured numbers above, which came from a separate,
  deliberately Mathlib-based, manually-run measurement — exactly the kind of thing gate 9 asks for a report on,
  not an assertion suite to pass.

## Sequencing constraints (spec §8)

The MVP roadmap is ordered by *what cannot be safely retrofitted*, not by what's easiest. The acceptance path
(seal/link/replay/audit) and the full obligation/attempt/verdict schema come before anything that uses them,
including before any model is wired in — Phase 2 ("null agent") makes zero model calls and must close the easy
tail of miniF2F deterministically at zero token cost, because it's the only way to later distinguish a broken
harness from a policy that needs tuning. If asked to add a feature, check the MVP/post-MVP tables in §8–9 before
assuming it belongs now — Ray, MCP wrappers, the zygote fork, `BestFirstDAG`, multi-tenancy, and the training
loop are all explicitly deferred, and pulling one forward without its prerequisites is a design smell, not a
convenience.
