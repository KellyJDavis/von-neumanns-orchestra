# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

Phase 0 (foundations) is complete and committed: `uv` workspace with stub packages, the `leankernel` Lake package
pinned to Mathlib v4.33.1, GitHub Actions CI, `deploy/Dockerfile.base`, a `deploy/grants.sql` role skeleton, and
`docs/provenance.md`. Phase 1 (the acceptance path) is in progress — M1.1 (Seal + Audit) is implemented and
tested; see "Implementation notes" below for load-bearing facts discovered while building it. Read
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
- `tests/db/` (once it exists) is meant to run against a real PostgreSQL instance, never a mock (spec §3) — don't
  introduce a mocked-DB test path.

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

**Deviation from the spec's literal layout**: `leankernel`'s own unit tests (`AuditFixtures.lean`, `Main.lean`
driving the `kernel_tests` executable) live under `packages/leankernel/LeanKernelTests/`, not the top-level
`tests/kernel/` the spec's tree sketch implies. Reason: `tests/kernel/` as a separate Lake package would need its
own `require mathlib`, duplicating a second multi-GB fetch/build of a dependency `packages/leankernel` already
resolves — co-locating tests inside the package that owns them avoids that for zero real cost, since the fixtures
are Mathlib-free anyway (see below). Reserve top-level `tests/kernel/` for adversarial developments that
genuinely need to sit outside any single package (e.g. gate 7's stratified Mathlib sample in M1.4).

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

These surfaced while building M1.1 (`packages/leankernel/LeanKernel/{Audit,Seal}.lean`) by testing directly
against the real v4.33.1 toolchain rather than assuming from memory. They are load-bearing for M1.2/M1.3 (Link,
Replay) and especially M1.8 (`leanserv`), which needs this exact capability — elaborating arbitrary fresh source
from a compiled process — in production, not just in tests.

- **`lean4checker` is deprecated**: merged into Lean itself as `leanchecker`, built into every toolchain since
  v4.28.0 (`lake env leanchecker`, or `--fresh` to replay into a fresh environment). Don't add it as a Lake
  dependency; the spec predates this merge.
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

## Sequencing constraints (spec §8)

The MVP roadmap is ordered by *what cannot be safely retrofitted*, not by what's easiest. The acceptance path
(seal/link/replay/audit) and the full obligation/attempt/verdict schema come before anything that uses them,
including before any model is wired in — Phase 2 ("null agent") makes zero model calls and must close the easy
tail of miniF2F deterministically at zero token cost, because it's the only way to later distinguish a broken
harness from a policy that needs tuning. If asked to add a feature, check the MVP/post-MVP tables in §8–9 before
assuming it belongs now — Ray, MCP wrappers, the zygote fork, `BestFirstDAG`, multi-tenancy, and the training
loop are all explicitly deferred, and pulling one forward without its prerequisites is a design smell, not a
convenience.
