# Lean Proof Agent — Engineering Specification v1.0

**Status:** Consolidated build specification
**Supersedes:** spec v0.1–v0.4 and review documents r1–r3. Those remain useful only as a record of why decisions were made; this document is what you build from.

A ground-up agentic Lean 4 theorem-proving system with functional parity to `project-numina/numina-lean-agent`, under five constraints: future-proofing, full open-sourceability including model weights, scalability to large proofs and many users, a well-supported backbone, and pluggable tools.

---

## 1. What the system does

Takes a Lean 4 file containing `sorry`s (or a bare statement), decomposes it into independently schedulable proof obligations, proves them with a mix of symbolic tactics and language models, and returns a file whose theorems are verified to prove exactly what was asked.

Everything else in this document follows from one invariant and one scheduling decision.

### 1.1 The core invariant

**An obligation becomes `proved` only when all four hold:**

1. **Link** — the kernel accepts the agent's term at the *sealed* goal's type.
2. **Replay** — the proof's dependency cone re-checks in a fresh kernel environment.
3. **Audit** — `collectAxioms` on the link passes the run's allowlist.
4. **Seal integrity** — the sealed module's digest matches the one recorded on the obligation.

The agent never writes the goal statement down. It is elaborated once, before the agent runs, in an environment the agent cannot influence, and frozen as a compiled constant. This makes statement drift structurally impossible rather than something to detect.

Enforced at three levels: a Lean metaprogram that forces its own kernel options, an independent re-checker (`lean4checker`), and a PostgreSQL `SECURITY DEFINER` function that is the only path to the `proved` status.

### 1.2 The core scheduling decision

**The unit of work is the proof obligation, not the file.** Obligations form a DAG with competing decomposition groups. This is what makes caching, resumption, budget allocation, backtracking, and multi-user fairness expressible; a file-oriented design supports none of them.

---

## 2. Tech stack

| Layer | Choice | Version floor | Rejected, and why |
|---|---|---|---|
| Orchestration language | Python | 3.12 | Rust (contributor pool), Go (ML ecosystem) |
| Package management | `uv` workspace | 0.5 | Poetry, pip-tools |
| Async runtime | `asyncio` + `uvloop` | 0.20 | trio (ecosystem), threads |
| Control loop | Postgres state machine, hand-written | — | Temporal (state already externalized), LangGraph (static topology), Prefect/Dagster (pipeline shape), Celery (second stateful system) |
| Distributed runtime | Ray *(post-MVP)* | 2.40 | Dask (weaker actors) |
| Persistence | PostgreSQL | 16 | SQLite (single-writer), MySQL |
| ORM / migrations | SQLAlchemy 2.0 async + Alembic | 2.0.36 | raw asyncpg (no migrations), Django ORM |
| Schemas | Pydantic v2 | 2.9 | dataclasses (no JSON Schema), attrs |
| HTTP server | FastAPI + uvicorn | 0.115 | Flask, Litestar |
| HTTP client | httpx | 0.27 | requests (sync), aiohttp |
| Blob store | local CAS → S3-compatible | — | Postgres large objects |
| Lean toolchain | pinned per run | **4.23 minimum**, 4.29+ recommended | — |
| Lean interaction | `leanprover-community/repl`, pooled | — | `lake env lean` per check, LeanDojo |
| REPL server | fork of Kimina Lean Server | — | bespoke pool |
| Proof re-checking | `lean4checker`; `lake check --paranoid` | Lean 4.35+ for paranoid | bespoke replay |
| Axiom audit | `leankernel` + `leanprover-community/axiom-audit` | — | hardcoded axiom name lists |
| Lean-side code | Lake package on Mathlib, `Lean.Elab` metaprograms | — | regex over source, tree-sitter |
| Model serving | vLLM primary; SGLang benchmarked from Phase 5 | vLLM 0.7 | TGI, llama.cpp |
| Model wire format | OpenAI-compatible `/v1/completions` with **token ids** | — | `/v1/chat/completions` (loses token identity), vendor SDKs |
| Constrained decoding | XGrammar | — | Outlines (compile latency) |
| Closed-API adapter | LiteLLM, pinned exactly, one module | — | vendor SDKs as primary |
| Tool protocol | MCP over streamable HTTP *(post-MVP)* | official `mcp` SDK | stdio MCP, bespoke REST |
| Vector index | `pgvector` + HNSW | 0.8 | Qdrant/Milvus at 2×10⁵ docs |
| Sandbox | bubblewrap → gVisor → Firecracker | — | Docker alone, chroot |
| Observability | OpenTelemetry → Langfuse/Phoenix; Prometheus | — | bespoke logging |
| RL training *(post-MVP)* | verl or OpenRLHF | — | TRL (weaker Ray/vLLM colocation) |
| Config | Pydantic Settings + TOML | — | Hydra (interpolation harms reproducibility) |
| Images | Docker, pinned by digest; optional Nix flake | — | Nix as default |
| Lint / types / test | ruff, mypy `--strict`, pytest, hypothesis | — | — |

**Candidate open-weights provers:** Goedel-Prover-V2 (8B), Kimina-Prover distills (1.7B/8B), DeepSeek-Prover-V2 (7B). All serve under vLLM and are interchangeable behind the router.

---

## 3. Repository layout

```
lean-agent/
├── pyproject.toml                    uv workspace root
├── uv.lock
├── packages/
│   ├── core/
│   │   └── src/lean_agent_core/
│   │       ├── schemas.py            Pydantic mirrors of every table
│   │       ├── protocols.py          LeanService, ModelBackend, Policy, ToolClient, BlobStore, Sink
│   │       ├── state.py              obligation state machine, acceptance predicate
│   │       ├── scheduler.py          claim, lease, heartbeat, reaper
│   │       ├── worker.py             the control loop
│   │       ├── budget.py             tokens / wallclock / kernel-seconds
│   │       ├── context.py            ContextBuilder, bands, eviction
│   │       └── blobs.py              content-addressed store
│   ├── leankernel/                   Lean 4 Lake package
│   │   ├── lakefile.lean
│   │   ├── lean-toolchain
│   │   └── LeanKernel/
│   │       ├── Audit.lean            collectAxioms + allowlist
│   │       ├── Seal.lean             goal bundle generation
│   │       ├── Link.lean             kernel link with forced options
│   │       ├── Replay.lean           lean4checker driver
│   │       ├── Sorries.lean          infotree → SorryGoal → Decomposition
│   │       ├── Infotree.lean         tactic step extraction
│   │       └── Main.lean             `lake exe leankernel <subcommand>`
│   ├── leanserv/
│   │   └── src/lean_agent_serv/
│   │       ├── pool.py               base-env-keyed REPL pool + LRU
│   │       ├── repl.py               REPL process wrapper, timeouts, crash taxonomy
│   │       ├── zygote.py             CoW fork from Mathlib parent (post-MVP)
│   │       ├── cache.py              L0/L1/L2/L3 tiers
│   │       ├── verdicts.py           writes verdict rows (own DB role)
│   │       └── api.py                FastAPI, internal
│   ├── models/
│   │   └── src/lean_agent_models/
│   │       ├── client.py             typed OpenAI-compatible client over httpx
│   │       ├── template.py           client-side chat template → token ids
│   │       ├── router.py             ModelRole → backend
│   │       ├── cache.py              response cache
│   │       └── adapters/closed.py    LiteLLM, isolated
│   ├── policies/
│   │   └── src/lean_agent_policies/
│   │       ├── symbolic.py           SymbolicPortfolio
│   │       ├── whole_proof.py        WholeProofSampler
│   │       ├── repair.py             RepairLoop
│   │       ├── decompose.py          DecomposeAndConquer
│   │       ├── informal.py           InformalFirst
│   │       ├── best_first.py         BestFirstDAG (post-MVP)
│   │       └── prompts/              versioned assets, hashed into the manifest
│   ├── tools/
│   │   └── src/lean_agent_tools/     direct clients in MVP
│   │       ├── search.py             Loogle, LeanExplore, pgvector
│   │       ├── informal.py
│   │       ├── transform.py          code golf, sorry2lemma, disprove
│   │       └── references.py         source-document retrieval
│   ├── mcp_search/                   post-MVP: same code, MCP wrapper
│   ├── mcp_informal/
│   ├── mcp_transform/
│   ├── mcp_references/
│   ├── api/
│   │   └── src/lean_agent_api/
│   │       ├── app.py                public FastAPI
│   │       ├── routes/               runs, obligations, attempts, base_envs, blobs
│   │       └── ingest.py             file → sealed goals → obligations
│   ├── eval/
│   │   └── src/lean_agent_eval/
│   │       ├── suites/               minif2f, putnam, sorrydb-style, internal
│   │       ├── score.py              pass@k with budget accounting
│   │       ├── reverify.py           clean-container re-verification
│   │       └── contamination.py
│   └── cli/
│       └── src/lean_agent_cli/       run, batch, from-folder, status, materialize
├── plugins/                          anything that cannot be open sourced
├── migrations/                       Alembic
├── deploy/
│   ├── Dockerfile.base               elan + Mathlib + warm `lake exe cache get`
│   ├── Dockerfile.serv
│   ├── Dockerfile.api
│   ├── compose.yaml                  MVP single-node
│   ├── ray/                          post-MVP cluster config
│   └── grants.sql                    privilege model (§5.5) — tested, not assumed
├── tests/
│   ├── db/                           run against a real PostgreSQL, never a mock
│   ├── kernel/                       adversarial Lean developments
│   └── e2e/
└── docs/
```

Every `packages/*` entry is independently publishable under Apache-2.0 with DCO. `plugins/` is excluded from the default install and from all benchmark configurations, enforced in CI.

---

## 4. The acceptance path

### 4.1 Seal

At obligation creation the goal source is elaborated **once**, in the base environment with a system-fixed option set, in a **warm** REPL worker. It is never compiled cold through `lake`: measured, cold per-child compilation is roughly 78% of pipeline time for a twenty-child group against under 1% warm.

```lean
-- LeanAgent/Goals/Bundle_<digest>.lean   (generated, sealed, read-only)
import <base environment>
set_option autoImplicit false
set_option relaxedAutoImplicit false
namespace LeanAgent.Goals
def G_<id₁>.{u_0} : Sort _ := <elaborated statement₁>
def G_<id₂>      : Prop    := <elaborated statement₂>
end LeanAgent.Goals
```

One bundle per decomposition group. The `.olean` artifact is produced lazily out of band for reproducibility and `lean4checker` batching; the hot path never waits on the build system.

`Sort _` where the goal is data-producing. Universe parameters are explicit at seal time. A remaining universe metavariable is an admission failure. **If the statement does not elaborate, no obligation is created**: the submission is rejected as `seal_failed` with diagnostics attached, which is distinct from any proof outcome.

### 4.2 Link

The agent must produce a declaration named `LeanAgent.Sol.sol_<id>`. That name is stated in the prompt and resolved by `Name`; a proof under a different name reports a name mismatch, not a proof failure.

```lean
def link (goal : Name) (entry : Name) : CoreM LinkReport := do
  let env ← getEnv
  let some idx := env.getModuleIdxFor? goal | throwError "goal constant absent"
  unless idx == (← sealedModuleIdx) do throwError "goal shadowed or redeclared"
  let some goalInfo  := env.find? goal  | throwError "goal missing"
  let some entryInfo := env.find? entry | throwError "entry point missing"

  -- universes come from the SEALED GOAL, never from the agent's declaration
  let lvls := goalInfo.levelParams
  unless entryInfo.levelParams.length == lvls.length do
    throwError "entry is not universe-polymorphic at the goal's arity"
  let lvlArgs := lvls.map mkLevelParam
  let type  := mkConst goal  lvlArgs
  let value := mkConst entry lvlArgs

  -- Prop goals are theorems; data-producing goals must be definitions
  let isProp ← Meta.isProp type
  let decl := if isProp
    then Declaration.thmDecl  { name := `LeanAgent.__link, levelParams := lvls, type, value }
    else Declaration.defnDecl { name := `LeanAgent.__link, levelParams := lvls, type, value,
                                hints := .opaque, safety := .safe }

  -- `Lean.addDecl` RESPECTS `debug.skipKernelTC`. Go to the kernel with our own options.
  let kenv := (← getEnv).toKernelEnv
  let opts := KVMap.empty.setBool `debug.skipKernelTC false
  let kenv' ← ofExceptKernelException (Lean.Kernel.Environment.addDecl kenv opts decl)
  modifyEnv fun _ => kenv'.toEnvironment
  auditAxioms `LeanAgent.__link (← allowlist)
```

Three properties: the goal constant cannot be redirected by `open`, `export`, or redeclaration; the kernel decides definitional equality, so `2 + 2` versus `4`, `abbrev` indirection, and alternative instance paths all pass without special handling; and weakening is impossible because the agent never writes the statement.

Two consequences to state so they are not rediscovered as bugs. A proof of a strictly *stronger* theorem does not link, because linking requires defeq rather than implication — the agent submits a derivation instead, since the entry point is an arbitrary term. And goals containing intentional metavariables (synthesis tasks) are out of scope.

### 4.3 Replay

`lean4checker` replays a module's environment from its imports and confirms the kernel accepts every declaration. It exists specifically to detect environment hacking. Mathlib runs it in CI. Use it; do not build a bespoke replay.

Trust base, stated because the pass does not otherwise imply it: replay does **not** re-check Mathlib. Its base is the pinned `.olean` set plus the sealed bundle, and `.olean` loading performs no kernel checking. Anyone who can write that tree wins. Defended by read-only mount plus the container image digest covering the whole tree.

Replay carries its own timeout and budget. Kernel reduction of a large `decide` can exceed the elaboration that produced it. **A replay timeout is not an acceptance**: it yields `verdict_kind = 'timeout'`, `replay_ok = false`, and the obligation stays open.

For published results and, under R18, for every accepted result, run `lake check --paranoid` across all kernel implementations the distribution ships. Independence across *implementations* is what buys something; two patch releases share an implementation and its bugs.

### 4.4 Audit

```lean
structure AxiomReport where
  decl : Name; axioms : Array Name
  usesSorry : Bool; usesCompilerTrust : Bool; ok : Bool
  deriving ToJson

def auditAxioms (decl : Name) (allow : Array Name) : CoreM AxiomReport
```

`sorryAx` fails unless `run.allow_sorry`. Any axiom outside the allowlist fails. Default allowlist `propext, Classical.choice, Quot.sound`, matching `leanprover-community/axiom-audit`.

**Allowlist, never deny-list, and property tests, never name lists.** Lean 4.29 changed native computation to one auto-generated axiom per computation with names like `..._native.bv_decide...`; `#print axioms` no longer reports `Lean.trustCompiler`. A deny-list would have silently stopped working at that release. CI compiles a `native_decide` proof and asserts rejection, with no axiom names in the test. Toolchain must be ≥ 4.23, below which `collectAxioms` under-reported by not walking axiom types.

`@[implemented_by]` alone cannot influence a kernel-checked term — but only because native evaluation is rejected outright, since compiler trust extends to every `@[extern]` and `@[implemented_by]` in scope.

### 4.5 Admission (roots only)

Sealing establishes identity, not quality. Admission runs on **submitted root obligations only**, never on decomposition children: a subgoal that `simp` closes is a good subgoal.

| Signal | Meaning |
|---|---|
| `simp`/`decide`/`exact?` closes it in < N s **[measure]** | Likely mis-formalization |
| Disprove search finds a counterexample | The statement is false |
| Elaborates only with `autoImplicit true` | Mistyped identifier silently generalized |
| Free universe metavariables after elaboration | Under-determined |
| No binder is used in the body | Possible vacuity **[measure]** |

Recorded on the obligation and reported in benchmark output.

### 4.6 Decomposition

```lean
structure SorryGoal where
  goalType : Expr; lctx : LocalContext
  pos : String.Range; suggestedName : Name

structure Decomposition where
  lemmas : Array (Name × Expr); reassembly : String
  deriving ToJson

def extractSorries : CommandElabM (Array SorryGoal)
def abstractSorry  (g : SorryGoal) : MetaM (Name × Expr × Expr)
def decompose      (src : String)  : CommandElabM Decomposition
```

Walks the `InfoTree` for `sorryAx` occurrences with their `LocalContext`, abstracts the context into binders via `mkForallFVars`, and emits closed standalone statements plus a reassembly term.

Three cases that must be handled and only surface on real Mathlib-dependent goals: instance-implicit binders must be re-bound as instance-implicit (detect via `BinderInfo`); unassigned universe metavariables must be generalized into universe parameters; and binders must be ordered by fvar dependency, not local-context order.

Every child is sealed at creation, so the local context is baked in and every obligation is closed by construction. **Reassembly is a full acceptance check**, not bookkeeping — the reassembly term against the proved children must link, replay, and audit against the parent's sealed goal. A failed reassembly fails the *group*; the children stay proved and reusable by another group.

---

## 5. Data models

### 5.1 Entity relationships

```
run ─┬─< obligation ─┬─< obligation_edge   (parent/child DAG, grouped)
     │               ├─< attempt ─┬─ verdict     (0..1, written by leanserv only)
     │               │            ├─< tool_call
     │               │            └─ trajectory  (0..1)
     │               └─ budget / spend (embedded columns)
     └─ manifest (embedded jsonb)

base_env            (materialized environments, shared across runs and tenants)
verification_cache  (content-addressed, global)
blob                (sha256 → CAS)
```

### 5.2 Enums

```sql
CREATE TYPE obligation_status AS ENUM (
  'open',         -- schedulable
  'in_progress',  -- one or more live attempts
  'decomposed',   -- children exist
  'proved',       -- terminal; requires the §1.1 predicate
  'failed',       -- budget exhausted
  'blocked',      -- every decomposition group has a failed child
  'abandoned'
);
CREATE TYPE attempt_status AS ENUM
  ('claimed','running','succeeded','failed','expired','infra_error');
CREATE TYPE verdict_kind AS ENUM
  ('proved','refuted','errors','timeout','oom','infra_error');
CREATE TYPE trust_class AS ENUM ('kernel_checked','advisory','retrieval');
CREATE TYPE provenance_class AS ENUM
  ('open_weights','symbolic','human','closed_api_eval_only');
```

`infra_error` is a first-class outcome and is **not** a proof failure. It does not charge `spent_attempts`. Conflating worker OOM with proof failure silently corrupts benchmark numbers and is the most common way these systems report a wrong pass rate.

### 5.3 Core tables

```sql
CREATE TABLE base_env (
  digest          bytea PRIMARY KEY,   -- sha256(toolchain ‖ mathlib ‖ recipe)
  recipe          jsonb NOT NULL,      -- imports, options, opens, ordered prelude
  toolchain_rev   text  NOT NULL,
  mathlib_rev     text  NOT NULL,
  snapshot_path   text,                -- L3 pickled environment
  snapshot_bytes  bigint,
  curated         boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now(),
  last_used_at    timestamptz
);

CREATE TABLE run (
  id              uuid PRIMARY KEY,
  tenant_id       uuid NOT NULL,
  base_env_digest bytea NOT NULL REFERENCES base_env(digest),
  status          text NOT NULL,
  manifest        jsonb NOT NULL,       -- §8; frozen at creation
  manifest_hash   bytea NOT NULL,
  allow_sorry     boolean NOT NULL DEFAULT false,
  axiom_allowlist text[] NOT NULL
    DEFAULT ARRAY['propext','Classical.choice','Quot.sound'],
  max_depth       int NOT NULL DEFAULT 6,
  budget_tokens       bigint,
  budget_wallclock_ms bigint,
  budget_kernel_ms    bigint,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON run (tenant_id, created_at DESC);

CREATE TABLE obligation (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id            uuid NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  status            obligation_status NOT NULL DEFAULT 'open',
  depth             int    NOT NULL DEFAULT 0,
  priority          double precision NOT NULL DEFAULT 0,
  is_root           boolean NOT NULL DEFAULT false,

  -- identity (§4)
  base_env_digest   bytea NOT NULL REFERENCES base_env(digest),
  goal_digest       bytea NOT NULL,   -- content address of the sealed goal; NOT identity
  sealed_olean_sha  bytea NOT NULL,   -- digest of the compiled bundle
  goal_src          text  NOT NULL,
  decl_name         text  NOT NULL,
  admission         jsonb NOT NULL DEFAULT '{}',   -- §4.5, roots only

  -- budgets
  budget_attempts   int    NOT NULL DEFAULT 8,
  budget_tokens     bigint NOT NULL DEFAULT 1000000,
  budget_kernel_ms  bigint NOT NULL DEFAULT 600000,
  spent_attempts    int    NOT NULL DEFAULT 0,
  spent_tokens      bigint NOT NULL DEFAULT 0,
  spent_kernel_ms   bigint NOT NULL DEFAULT 0,

  proof_blob        bytea,
  created_by_policy text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX obligation_schedulable
  ON obligation (run_id, status, priority DESC, depth)
  WHERE status = 'open';
CREATE INDEX ON obligation (goal_digest);

CREATE TABLE obligation_edge (
  parent_id  uuid NOT NULL REFERENCES obligation(id) ON DELETE CASCADE,
  child_id   uuid NOT NULL REFERENCES obligation(id) ON DELETE CASCADE,
  group_id   uuid NOT NULL,          -- one decomposition attempt = one group
  role       text NOT NULL,          -- 'subgoal' | 'lemma'
  reassembly_blob bytea,             -- on the group, not the child
  PRIMARY KEY (parent_id, child_id, group_id)
);
CREATE INDEX ON obligation_edge (child_id);
CREATE INDEX ON obligation_edge (group_id);
```

`group_id` is what makes backtracking expressible: a parent may hold several competing decompositions, and is proved when all children of **any one** group are proved *and the group's reassembly links*. A cycle guard runs in the same transaction that inserts an edge — a child's `goal_digest` may not equal any ancestor's, and `depth` is capped by `run.max_depth`. Without it, a policy can decompose an obligation into itself and consume budget forever.

```sql
CREATE TABLE attempt (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  obligation_id      uuid NOT NULL REFERENCES obligation(id) ON DELETE CASCADE,
  run_id             uuid NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  status             attempt_status NOT NULL DEFAULT 'claimed',
  policy_id          text  NOT NULL,
  policy_config_hash bytea NOT NULL,
  lease_owner        text,
  lease_expires_at   timestamptz,
  heartbeat_at       timestamptz,
  started_at         timestamptz NOT NULL DEFAULT now(),
  finished_at        timestamptz,
  tokens_in    bigint NOT NULL DEFAULT 0,
  tokens_out   bigint NOT NULL DEFAULT 0,
  kernel_ms    bigint NOT NULL DEFAULT 0,
  wallclock_ms bigint NOT NULL DEFAULT 0
);
CREATE INDEX attempt_expired ON attempt (lease_expires_at)
  WHERE status IN ('claimed','running');
CREATE INDEX ON attempt (obligation_id, started_at DESC);

CREATE TABLE verdict (
  attempt_id    uuid PRIMARY KEY REFERENCES attempt(id) ON DELETE CASCADE,
  obligation_id uuid NOT NULL REFERENCES obligation(id) ON DELETE CASCADE,
  kind          verdict_kind NOT NULL,
  link_ok         boolean NOT NULL,     -- §4.2
  replay_ok       boolean NOT NULL,     -- §4.3
  axiom_audit_ok  boolean NOT NULL,     -- §4.4
  sealed_olean_sha_observed bytea,      -- must equal obligation.sealed_olean_sha
  axioms        text[],
  kernels_agreeing text[],              -- R18: which kernels accepted
  messages_blob bytea, infotree_blob bytea, proof_blob bytea,
  elapsed_ms    bigint  NOT NULL,
  cache_hit     boolean NOT NULL DEFAULT false,
  toolchain_rev text NOT NULL, mathlib_rev text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON verdict (obligation_id, kind);

CREATE TABLE trajectory (
  attempt_id     uuid PRIMARY KEY REFERENCES attempt(id) ON DELETE CASCADE,
  provenance     provenance_class NOT NULL,     -- no default, by design
  model_id       text, model_weights_hash text, tokenizer_revision text,
  sampling       jsonb NOT NULL,
  seed           bigint,
  steps_blob     bytea NOT NULL,   -- JSONL of rendered steps
  token_ids_blob bytea,            -- prompt + completion token ids, per request (M3.10)
  logprobs_blob  bytea,            -- sampled-token logprobs, float32, per request
  n_steps        int NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON trajectory (provenance, model_id);

CREATE TABLE tool_call (
  id          bigserial PRIMARY KEY,
  attempt_id  uuid NOT NULL REFERENCES attempt(id) ON DELETE CASCADE,
  step_index  int  NOT NULL,
  server      text NOT NULL, tool text NOT NULL,
  trust       trust_class NOT NULL,
  args_blob   bytea NOT NULL, result_blob bytea,
  ok          boolean NOT NULL, latency_ms int NOT NULL,
  cost_usd    numeric(12,6) NOT NULL DEFAULT 0
);
CREATE INDEX ON tool_call (attempt_id, step_index);

CREATE TABLE verification_cache (
  cache_key     bytea PRIMARY KEY,
  kind          verdict_kind NOT NULL,
  axioms        text[],
  messages_blob bytea, infotree_blob bytea,
  elapsed_ms    bigint NOT NULL,
  toolchain_rev text NOT NULL, mathlib_rev text NOT NULL,
  hits          bigint NOT NULL DEFAULT 0,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_hit_at   timestamptz
);

CREATE TABLE blob (
  sha256     bytea PRIMARY KEY,
  size_bytes bigint NOT NULL,
  media_type text NOT NULL,
  location   text NOT NULL,          -- 'file://…' or 's3://…'
  tenant_id  uuid,                   -- visibility scope; content dedup is global
  created_at timestamptz NOT NULL DEFAULT now()
);
```

**Cache key.**

```
cache_key = sha256(base_env_digest ‖ sha256(declaration_source) ‖ canonical(check_options))
```

`base_env_digest` covers toolchain, Mathlib revision, imports, options, opens, and the ordered project prelude digesting **every command** — not only declarations, because an `@[simp]` attribute on an earlier lemma changes how `simp` behaves afterwards and a `set_option` changes elaboration. A key that covers only declaration sources collides across different accumulated environments and returns a verdict computed elsewhere.

Anything above 64 KiB **[measure]** goes to the CAS, never to Postgres. Rows hold `bytea` digests only.

### 5.4 Pydantic mirror

Every table has a frozen Pydantic v2 model in `packages/core/schemas.py`. These are the single source of truth for the OpenAPI document, MCP tool schemas, and on-disk JSONL. **No dict crosses a process boundary untyped.**

### 5.5 Privilege model

This is enforcement, not documentation, and the form matters. Tested against PostgreSQL 16: a table-level `GRANT UPDATE` confers update on every column, and a subsequent column-level `REVOKE` **does not subtract from it**. Grant-only.

```sql
-- deploy/grants.sql

-- Application (API + agent workers)
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app;
GRANT INSERT ON attempt, tool_call, trajectory, obligation, obligation_edge TO app;
GRANT UPDATE (priority, spent_attempts, spent_tokens, spent_kernel_ms, updated_at)
  ON obligation TO app;                       -- never GRANT UPDATE ON obligation
GRANT UPDATE ON attempt TO app;
REVOKE INSERT ON verdict FROM app;            -- table-level revoke DOES work
GRANT EXECUTE ON FUNCTION mark_proved TO app;

-- Lean Execution Service: the only writer of verdicts
GRANT SELECT ON ALL TABLES IN SCHEMA public TO leanserv;
GRANT INSERT ON verdict TO leanserv;
GRANT INSERT, UPDATE ON verification_cache, base_env, blob TO leanserv;
```

```sql
CREATE FUNCTION mark_proved(p_obligation uuid, p_attempt uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public        -- omitting this is an escalation vector
AS $$
BEGIN
  UPDATE obligation o SET status = 'proved', updated_at = now()
  FROM verdict v
  WHERE o.id = p_obligation
    AND o.status IN ('open','in_progress','decomposed')
    AND v.attempt_id = p_attempt AND v.obligation_id = o.id
    AND v.kind = 'proved'
    AND v.link_ok AND v.replay_ok AND v.axiom_audit_ok
    AND v.sealed_olean_sha_observed = o.sealed_olean_sha;
  IF FOUND THEN RETURN; END IF;
  -- Concurrent attempts on one obligation are normal; a second success is a no-op.
  IF EXISTS (SELECT 1 FROM obligation WHERE id = p_obligation AND status = 'proved') THEN
    RETURN;
  END IF;
  RAISE EXCEPTION 'acceptance predicate not satisfied for % / %', p_obligation, p_attempt;
END $$;
```

The trusted computing base for the core invariant is `leanserv`'s verdict-writing path plus this function. Workers request a check and observe the outcome; they never transcribe it.

---

## 6. Services and APIs

### 6.1 Public API (`packages/api`, FastAPI)

All request and response bodies are Pydantic models; the OpenAPI document is generated, not written.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/runs` | Create a run from a submission |
| `GET` | `/v1/runs/{run_id}` | Status, spend, counts by obligation status |
| `GET` | `/v1/runs/{run_id}/obligations` | Paginated; filter by `status`, `depth` |
| `GET` | `/v1/runs/{run_id}/events` | SSE stream of state transitions |
| `GET` | `/v1/runs/{run_id}/artifact` | Materialized `.lean` file (§6.3) |
| `GET` | `/v1/runs/{run_id}/manifest` | Frozen run manifest |
| `POST` | `/v1/runs/{run_id}/cancel` | Cooperative cancel; live attempts finish or expire |
| `GET` | `/v1/obligations/{id}` | Including `admission` signals and budget spend |
| `GET` | `/v1/obligations/{id}/dag` | Sub-DAG with groups and edge roles |
| `GET` | `/v1/obligations/{id}/attempts` | |
| `GET` | `/v1/attempts/{id}` | |
| `GET` | `/v1/attempts/{id}/trajectory` | Rendered prompts, completions, tool calls, verdict |
| `GET` | `/attempts/{id}` | The read-only trajectory viewer (§7.4): the same data as HTML, prompts decoded from their stored token ids *(M3.11)* |
| `GET` | `/v1/base-envs` | Curated and registered base environments |
| `POST` | `/v1/base-envs` | Register a project prelude; returns `digest` |
| `GET` | `/v1/blobs/{sha256}` | Tenant-scoped |
| `GET` | `/healthz`, `/readyz`, `/metrics` | Liveness, readiness, Prometheus |

```python
class CreateRunRequest(BaseModel):
    source: str | None = None          # .lean file contents with sorries
    statement: str | None = None       # or a bare goal
    base_env: str                      # digest or curated alias, e.g. "mathlib-stable"
    policy: str = "DecomposeAndConquer"
    budget: Budget
    allow_sorry: bool = False
    axiom_allowlist: list[str] | None = None
    max_depth: int = 6
    reference_docs: list[str] = []     # blob digests

class CreateRunResponse(BaseModel):
    run_id: UUID
    manifest_hash: str
    root_obligations: list[UUID]
    admission: dict[UUID, AdmissionReport]   # §4.5 signals, non-blocking
    seal_failures: list[SealFailure]         # statements that did not elaborate
```

`seal_failures` is returned rather than raised: a submission with ten goals of which one does not elaborate creates nine obligations and reports the tenth, instead of failing the whole request.

### 6.2 Lean Execution Service (`packages/leanserv`, internal only)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/check` | Elaborate a body against a base env |
| `POST` | `/v1/check_batch` | N bodies, one base env; amortizes worker acquisition |
| `POST` | `/v1/seal` | Elaborate and freeze a goal bundle |
| `POST` | `/v1/link` | §4.2, then replay and audit; writes the `verdict` row |
| `POST` | `/v1/replay` | Standalone `lean4checker` / `lake check --paranoid` |
| `POST` | `/v1/decompose` | `sorry` extraction → `Decomposition` |
| `POST` | `/v1/base-env/materialize` | Build and snapshot a base environment |
| `GET` | `/v1/health` | Pool occupancy, LRU hit rate, warm counts per base env |

```python
class CheckRequest(BaseModel):
    base_env_digest: str
    body: str
    options: CheckOptions
    timeout_ms: int = 300_000
    want: list[Literal["messages","infotree","axioms","sorries"]] = ["messages"]

class LinkRequest(BaseModel):
    attempt_id: UUID                 # leanserv writes the verdict against this
    obligation_id: UUID
    base_env_digest: str
    bundle_sha: str
    goal: str                        # LeanAgent.Goals.G_<id>
    entry: str                       # LeanAgent.Sol.sol_<id>
    development: str
    paranoid: bool = False           # multi-kernel replay

class LinkResponse(BaseModel):
    kind: VerdictKind
    link_ok: bool; replay_ok: bool; axiom_audit_ok: bool
    axioms: list[str]; kernels_agreeing: list[str]
    elapsed_ms: int; cache_hit: bool
```

`want` exists so callers pay only for what they use; infotree extraction is expensive and most checks do not need it.

**Worker model.** One REPL process per worker, keyed by `base_env_digest`, LRU over base environments.

| Parameter | Starting value | Note |
|---|---|---|
| Memory cap per worker | 12 GiB **[measure]** | Mathlib import dominates |
| Warm workers per hot base env | 4 **[measure]** | Tune to p99 queue depth |
| LRU capacity | `floor(RAM / cap) − reserve` | ~21 workers, ~5 base envs at 256 GiB |
| Per-command wallclock | 300 s, per-request override | External SIGKILL |
| `maxHeartbeats` | 400000 **[measure]** | In-Lean bound |
| REPL `gc` | `true` | Bounds memory growth |

Timeouts need **both** mechanisms. `maxHeartbeats` does not bound `native_decide`'s compiler invocation, deep `decide` recursion, or elaboration-time `IO`.

**Header fragmentation is the binding multi-tenancy constraint.** At these numbers a 256 GiB node supports about five concurrently warm base environments. Arbitrary user import lists fragment the pool catastrophically, so runs select imports from a curated set and anything else is routed to a slower pool with its own quota. Project-local libraries are handled as *preludes* within a base environment, which removes re-elaboration cost — but **not** memory cost, since a prelude-bearing worker occupies a full slot. The fix for that is the zygote (§9.2), which is post-MVP.

### 6.3 Ingestion and materialization

1. **Submit** a `.lean` file, a bare statement, or a `lake` target.
2. **Resolve base environment.** Imports matched against the curated set; a mismatch names the nearest blessed environment rather than silently fragmenting the pool.
3. **Elaborate once**, extract `sorry` sites (§4.6).
4. **Seal** each site's goal into a bundle; create one obligation per site with edges recording reassembly.
5. **Prove.** Obligations schedule independently.
6. **Materialize.** Write the file with each `sorry` replaced, then check **two** things: the assembled file elaborates, *and* each top-level declaration corresponding to a submitted goal **links** against its sealed constant with replay and audit.

Step 6's second check is not optional. Elaborating the file proves it compiles, not that it proves what was asked; without the link the materialized file could compile cleanly with a drifted statement. Per-obligation acceptance proves each hole is filled correctly; whole-file verification proves they were filled *compatibly*.

### 6.4 Control loop

```python
async def worker(worker_id: str, deps: Deps) -> None:
    while not deps.shutdown.is_set():
        attempt = await claim_attempt(deps.db, worker_id, lease=timedelta(seconds=60))
        if attempt is None:
            await asyncio.sleep(backoff.next()); continue
        with heartbeat_thread(deps.dsn, attempt.id, every=timedelta(seconds=20)):
            try:
                ctx    = await load_context(deps, attempt)
                policy = deps.policies[attempt.policy_id]
                result = await run_policy(policy, ctx, attempt, deps)
            except InfraError as e:
                await commit_infra_error(deps.db, attempt, e)   # no budget charged
            else:
                await commit(deps.db, attempt, result)
```

The heartbeat runs on a **dedicated thread with its own connection**, not as an asyncio task: a blocking tokenizer call or CPU-bound serialization inside a policy would otherwise starve it and get a live worker reaped. Session-level advisory locks were considered and rejected — they would pin one backend connection per in-flight attempt, which with hour-long attempts caps concurrency at `max_connections` to buy seconds of detection latency.

Lease expiry means *the worker is gone*, never *the attempt took too long*. Wallclock is a budget concern.

```sql
WITH next AS (
  SELECT o.id FROM obligation o JOIN run r ON r.id = o.run_id
  WHERE o.status = 'open' AND o.spent_attempts < o.budget_attempts
    AND r.status = 'running'
    AND r.tenant_id = ANY($eligible_tenants)   -- refreshed by the admission loop
  ORDER BY o.priority DESC, o.depth ASC, o.created_at ASC
  FOR UPDATE OF o SKIP LOCKED LIMIT 1
),
locked AS (
  UPDATE obligation o SET status = 'in_progress', updated_at = now()
  FROM next WHERE o.id = next.id RETURNING o.id, o.run_id
)
INSERT INTO attempt (id, obligation_id, run_id, policy_id, policy_config_hash,
                     lease_owner, lease_expires_at, status)
SELECT gen_random_uuid(), locked.id, locked.run_id, $policy, $cfg_hash,
       $worker_id, now() + $lease, 'claimed'
FROM locked RETURNING *;
```

Quota is checked coarsely here and exactly at commit; over-admission by one attempt is acceptable, a per-row function call on a locking scan is not. `ORDER BY … SKIP LOCKED` can invert priority under contention; documented, and shardable by `hashtext(id)` if it becomes visible.

**State machine.**

```
open ──claim──▶ in_progress ──┬── §1.1 predicate ────────────▶ proved
                              ├── Decompose ─────────────────▶ decomposed
                              ├── errors / timeout ──────────▶ open  (attempts++)
                              ├── budget exhausted ──────────▶ failed
                              └── infra_error ───────────────▶ open  (no charge)

decomposed ──group's children proved ∧ reassembly links──────▶ proved
           ──every group has a failed child──────────────────▶ blocked
```

There is no "all children proved implies parent proved" shortcut. Reassembly produces a real attempt with a real link and flows through `mark_proved` like anything else.

### 6.5 Model layer

**Open-weights backends use `/v1/completions` with token ids.** Render the chat template client-side with `AutoTokenizer.apply_chat_template(tokenize=True)` against a tokenizer pinned by revision, and send the id list. Sending a string is not enough: `tokenize=False` output does not re-tokenize to the identity around special tokens and whitespace. Server-side templating loses the exact token sequence entirely, which breaks replay and on-policy RL.

**Request `logprobs` on every sampled token and store them.** Recomputing behavior-policy logprobs later produces the train/inference mismatch. Store the sampled token's logprob as float32 — about 4 GB per 10⁹ tokens — not top-k.

Cost: tool-call parsing must be reimplemented per model family, since the server's tested parsers are bypassed. Constrain output with XGrammar and keep a conformance suite against server-side behavior on a fixed corpus.

```python
class ModelRole(StrEnum):
    PROVER = "prover"; DECOMPOSER = "decomposer"; CRITIC = "critic"
    FORMALIZER = "formalizer"; INFORMAL = "informal"
```

Policies request a **role**, never a model. Switching provers is one TOML line; a four-model ablation is four config files and no code. Response cache keyed on `sha256(prompt_tokens) ‖ model_id ‖ canonical(sampling) ‖ seed`.

### 6.6 Policies and context

| Policy | Roles | Purpose |
|---|---|---|
| `SymbolicPortfolio` | none | `exact?`, `apply?`, `rw?`, `aesop`, `simp_all`, `omega`, `bv_decide`, `decide`, `linarith`, `nlinarith`, `polyrith`, `norm_num`, `field_simp`, timed portfolio, zero tokens |
| `WholeProofSampler` | prover | Sample *n* proofs at temperature *t*, check all |
| `RepairLoop` | prover | Sample, check, feed infotree-localized diagnostics back, resample |
| `DecomposeAndConquer` | decomposer, prover | Skeleton with `sorry`s → decompose → schedule → reassemble |
| `InformalFirst` | informal, formalizer, prover | NL proof, critique, then formalize |
| `BestFirstDAG` *(post-MVP)* | any | Non-uniform budget allocation over the DAG |

```python
Action = (SubmitProof | Decompose | CallTool | RequestCompletion | Abandon)

class Policy(Protocol):
    id: str; config_hash: bytes
    tools: frozenset[str]; roles: frozenset[ModelRole]
    def propose(self, ctx: ObligationContext, budget: Budget
                ) -> AsyncGenerator[Action, Observation | None]: ...

Observation = CheckOutcome | CompletionResponse   # sent back after each action
```

The executor performs side effects, not the policy. That is what keeps trajectories replayable and the tool allowlist enforceable. After performing an action the executor **sends back what it observed** — the completion for a `RequestCompletion`, the check result for a screened-out `SubmitProof` — so `response = yield RequestCompletion(...)` is how a policy reads its samples. *(Revised in M3.9 from `AsyncIterator[Action]`, which has no channel back: `WholeProofSampler` cannot submit samples it is never shown, and `RepairLoop`'s "feed diagnostics back" is the definition of one. A sent value is one recorded input per action, which a replay can supply; a mutable observations object would be hidden state it would have to reconstruct.)*

**Context bands**, in eviction priority:

| Band | Content | Budget |
|---|---|---|
| 1 | Sealed goal, pretty-printed, plus base env | Never evicted |
| 2 | Kernel diagnostics, infotree-localized | Hard 30% **[measure]** |
| 3 | Retrieved premises, ranked | Truncated by rank |
| 4 | Proved ancestors and siblings, statements only | Statements before proofs |
| 5 | Error history, deduplicated by class | Oldest first |
| 6 | Reference-document passages | Lowest rank first |

Two rules matter more than the bands. **Never summarize kernel output** — truncate instead: keep the error head, truncate the goal state by hypothesis, elide the middle with an explicit size marker so the model can request more rather than hallucinate over the gap. And **keep bands 1–2 byte-stable across resamples**, or prefix caching is defeated and cost multiplies silently.

Prompts under `packages/policies/prompts/` are versioned assets hashed into the manifest. The existing coordinator and autosearch prompts are MIT-licensed and empirically tuned: port them verbatim and treat divergence as a measured change.

---

## 7. Cross-cutting concerns

### 7.1 Provenance

The binding constraint on open weights is not code licensing; it is the provenance of the training data. Encoded in the schema:

- `trajectory.provenance` is `NOT NULL` with **no default**.
- It is derived from `ModelBackend.provenance` at registration, never asserted by the caller.
- `symbolic` is refused by the executor on any attempt with a nonzero completion count. A model-guided tactic choice is not a symbolic trajectory however few tokens it used.
- The corpus exporter **raises** on any trajectory outside `{open_weights, symbolic, human}`, listing offending ids. It does not filter.

Bootstrapping by distilling a closed model produces encumbered weights. `SymbolicPortfolio` matters beyond being a baseline: it produces unencumbered training data at zero token cost.

### 7.2 Security

Lean is a general-purpose language and elaboration runs arbitrary `IO`. Untrusted `.lean` input can execute code via `#eval`, `initialize`, and macro expansion; invoke the compiler through `native_decide`; make `lake` fetch and run build scripts; and exhaust resources in ways `maxHeartbeats` does not bound. Lean's own guidance classifies un-reviewed AI-generated proofs as *malicious* rather than *honest* code, which is exactly this system's output.

| Deployment | Isolation |
|---|---|
| Single-node research (MVP) | bubblewrap: user namespace, seccomp, no network namespace |
| Multi-tenant shared | gVisor (`runsc`) under containerd |
| Public service | Firecracker microVMs, one per tenant pool |

```
/opt/lean     (ro)  elan toolchains, pinned by digest
/opt/mathlib  (ro)  Mathlib source + prebuilt .lake artifacts
/opt/goals    (ro)  sealed bundles; digest verified at check time, not trusted from the mount
/work         (rw)  overlayfs upper, tmpfs, per-attempt, discarded
/tmp          (rw)  tmpfs, size-capped
```

No network except a unix socket to the tool proxy. `lake` never has network access in a serving worker; dependency resolution happens at image build time.

**Option deny-list**, defense in depth behind replay:

| Option | Effect | Handling |
|---|---|---|
| `debug.skipKernelTC` | Disables kernel type-checking | Denied; link forces it false; replay independent |
| `debug.byAsSorry` | Replaces tactic scripts with `sorry` | Denied; audit catches regardless |
| `autoImplicit`, `relaxedAutoImplicit` | Silently generalizes unbound identifiers | Forced false in sealed goals; flagged in admission |
| `maxHeartbeats 0` | Removes the in-Lean bound | Denied; external SIGKILL remains |
| `maxRecDepth` | Stack exhaustion | Capped, not denied |

The guarantee comes from replay, not the list. Design so it does not depend on enumerating something that will grow.

**Multi-tenancy.** Row-level security on `tenant_id`; blob visibility tenant-scoped while content dedup stays global. The verification cache is deliberately **global**: with the §5.3 key it is a pure function of its inputs, and a hit implies the requester already holds the content. The residual is a timing channel — a tenant can learn a declaration source was previously checked. Deployments that cannot accept it partition the cache and pay warm-up.

### 7.3 Reproducibility

The run manifest is frozen at creation and included in every published result:

```json
{
  "schema_version": "1.0",
  "code":      { "git_rev": "…", "dirty": false, "uv_lock_hash": "sha256:…" },
  "container": { "image": "ghcr.io/<org>/lean-agent", "digest": "sha256:…" },
  "lean":      { "toolchain": "leanprover/lean4:v4.29.0", "mathlib_rev": "…",
                 "leankernel_rev": "…", "repl_rev": "…", "lean4checker_rev": "…" },
  "base_env":  { "digest": "sha256:…", "alias": "mathlib-stable" },
  "models":    [{ "role": "prover", "backend": "vllm",
                  "model_id": "…", "weights_revision": "…",
                  "tokenizer_revision": "…", "serving_version": "vllm==0.7.3",
                  "context_tokens": 40960, "provenance": "open_weights",
                  "sampling": { "temperature": 0.8, "top_p": 0.95, "seed": 1234 } }],
  "policies":  [{ "id": "DecomposeAndConquer", "config_hash": "…",
                  "prompt_hashes": { "coordinator": "sha256:…" } }],
  "budgets":   { "tokens": 50000000, "kernel_ms": 86400000 },
  "axiom_allowlist": ["propext","Classical.choice","Quot.sound"],
  "allow_sorry": false
}
```

| Level | Guarantee | Scope |
|---|---|---|
| **R0 Verdict-identical** | Same obligations, same goal digests, same verdicts | All runs |
| **R1 Token-identical** | Identical prompt and completion token ids | CI canary only: fixed hardware, serving version, seed, batch composition |
| **R2 Bitwise** | Identical logits | Not offered |

R1 is a *verification affordance*, not a mechanism anything depends on. Batching perturbs floating-point reduction order, and different GPUs or a vLLM upgrade break it. Training uses stored token ids and logprobs (§6.5); replay detects divergence and reports a diff.

### 7.4 Observability

OpenTelemetry spans per run, obligation, attempt, policy step, completion, tool call, and Lean check, propagated into tool calls by header. Postgres remains the source of truth and the system must run with observability disabled.

Domain metrics that will actually drive decisions:

| Metric | Why |
|---|---|
| Kernel-seconds per proved obligation | True unit cost; the fair-share unit |
| Tokens per proved obligation, by policy | Where the model budget goes |
| Cache hit rate by tier (L0/L1/L3) | Predicts marginal cost at scale |
| Base-env LRU eviction rate | Early warning for pool fragmentation |
| Decomposition fan-out and reassembly success rate | Whether decomposition helps or thrashes |
| Attempts rejected by the axiom audit | Near zero; a spike means a policy found a hole |
| Link failures where replay disagrees with elaboration | Must be zero; nonzero is an attempted weakening |
| Kernel disagreement events | R18; escalate as a security incident |
| `infra_error` rate | Corrupts benchmark numbers if it drifts unnoticed |
| Admission signals per benchmark | Formalization quality, independent of the agent |

A read-only trajectory viewer over `trajectory`, `tool_call`, and `verdict` showing exact rendered prompts (not reconstructions) is the primary debugging tool and must exist by MVP Phase 3, not at the end.

### 7.5 Evaluation

| Suite | Purpose |
|---|---|
| Internal regression | Symbolic-only, deterministic, zero tokens, every PR |
| miniF2F | Calibration against published numbers |
| PutnamBench | Competition difficulty |
| SorryDB-style repository sorries | The capability actually wanted |
| Paper-level formalization | End to end, scored on obligations closed |

Scoring discipline: report pass@k **with the budget that produced it** (tokens, kernel-seconds, wallclock, sample count); report the `infra_error` rate alongside; re-verify every claimed success from scratch in a clean container from the manifest via `lake check --paranoid`; and hold the benchmark's own sealed goal bundle read-only so proving a weakened restatement is structurally impossible rather than something to detect.

### 7.6 Toolchain migration

Mathlib breaks downstream code continuously, and every bump invalidates the whole verification cache.

- Runs pin `mathlib_rev`; a run never migrates mid-flight.
- A **bump run** is a first-class operation: rebuild at a new `(toolchain, mathlib_rev)`, recompile sealed bundles, re-verify the proved corpus by replay.
- The report classifies each breakage as *seal broke*, *proof broke*, or both. These need different responses.
- Cache entries are retained keyed by old revision, because bisecting a regression needs the old verdicts. GC by age and hit count.

This is cheap only because obligations are content-addressed and replay is elaboration-independent. A file-oriented system re-runs the agent to find out what broke; this one re-runs the kernel.

---

## 8. MVP roadmap

**MVP definition.** A single-node system that takes a `.lean` file with `sorry`s, decomposes it, proves what it can with a symbolic portfolio plus one open-weights model, and materializes a file whose theorems link against sealed goals. **Ten weeks**, one to two engineers with Lean metaprogramming experience.

What is in the MVP is decided by one test: **can this be retrofitted without breaking something?** If not, it ships in the MVP however unglamorous.

| In the MVP | Why it cannot wait |
|---|---|
| Seal / link / replay / audit | The only part where a defect is unrecoverable |
| Full obligation/attempt/verdict schema | Schema is architecture |
| Privilege model and `mark_proved` | Retrofitting enforcement onto code that violates it does not work |
| `trajectory.provenance` `NOT NULL` | Cannot be reconstructed from logs a year later |
| Token ids + logprobs at generation | Cannot be recomputed correctly later |
| Warm REPL pool, base-env keyed | Determines whether anything else is affordable |
| Content-addressed verification cache | Cache keys are hard to change once data exists |
| Decomposition and the DAG | The system's actual differentiator |
| bubblewrap sandbox | Trivial now, awkward later |

| Deferred | Why it is safe to defer |
|---|---|
| Ray | Deployment topology only; `packages/core` unaffected |
| MCP servers | Same tool code behind a wrapper; add when a second consumer exists |
| Zygote fork | Needed for multi-tenancy, not for one user |
| L3 snapshot persistence | Warm sealing needs a warm worker, not a persisted snapshot |
| `BestFirstDAG`, competing groups | Scheduler policy over an unchanged schema |
| Multi-tenancy, RLS, quotas, gVisor | Additive |
| `lake check --paranoid` | Single `lean4checker` in MVP; paranoid before publishing anything |
| Training loop | Needs everything above first |

### Phase 0 — Foundations (1 week)

uv workspace, CI (ruff, `mypy --strict`, pytest), base image with elan + Mathlib + warm `lake exe cache get`, provenance policy written down, `deploy/grants.sql`.

**Exit:** CI green; `lake build` succeeds against pinned Mathlib; no `packages/*` module imports `plugins/`.

### Phase 1 — Acceptance path (4 weeks) — *the schedule risk*

`leankernel` (audit, seal, link, replay driver, sorries, infotree), `leanserv` (pool, cache L0–L2, HTTP API, verdict writing), full DDL with Alembic, blob store, eval harness skeleton.

Build the acceptance predicate **first**, before anything that uses it. It is small, it is the only place a defect is unrecoverable, and both critical findings in review were here.

**Exit gates:**

| # | Assertion |
|---|---|
| 1 | 10,000 Lean Workbook proofs sealed, linked, replayed, audited; report throughput, L1 hit rate on repeat (>95% **[measure]**), replay cost as a fraction of elaboration |
| 2 | Axiom audit detects every known-`sorry` declaration; zero false negatives |
| 3 | A `native_decide` proof is rejected, with **no hardcoded axiom names** in the test |
| 4 | `link` rejects a development setting `debug.skipKernelTC`, **with replay disabled** — proving the link's own defense works, not just the backstop |
| 5 | A defeq-but-textually-different proof links; a proof of a weakened mutant does not |
| 6 | A universe-polymorphic goal links only at matching arity; a `Type`-valued goal links |
| 7 | Decomposition round-trip on a Mathlib sample (hypothesis fuzz): children link standalone, reassembly links against the parent, no new axioms |
| 8 | Privilege model holds **against a live PostgreSQL, not a mock**: status bypass refused, permitted-column update allowed, worker `INSERT INTO verdict` refused, repeated `mark_proved` idempotent |
| 9 | Prelude memory delta measured (R19 input) |

Gates 4, 6, and 8 exist because the corresponding code failed the first time it was actually executed.

### Phase 2 — Null agent (2 weeks)

Control loop, lease, heartbeat thread, reaper, scheduler, `SymbolicPortfolio`, ingestion and materialization. **Zero model calls anywhere in the codebase.**

**Exit:** closes the easy tail of miniF2F deterministically in CI at zero token cost, stable across three runs; a materialized file passes both the elaboration and the link check. This suite runs on every PR forever and is the only way to later distinguish a broken harness from a policy that needs tuning.

### Phase 3 — Model as policy (2 weeks)

`ModelRouter`, typed completions client with client-side token-id templating, response cache, trajectory logging with provenance and logprobs, `WholeProofSampler`, `RepairLoop`, `ContextBuilder`, trajectory viewer.

**Exit:** the Phase 2 symbolic baseline still passes bit-identically and the model policy strictly dominates it; reproduce the *relative ranking* of three open-weights provers on miniF2F; match one published absolute number within a stated tolerance, with a written account of any gap. Corpus exporter raises on a mixed-provenance set.

Ranking is the load-bearing criterion. Published absolute numbers depend on unreported harness details, so a gap may indicate nothing; ranking is robust to that.

### Phase 4 — Decomposition and tools (1 week)

`DecomposeAndConquer`, in-process tool clients (search, informal, transform, references), `InformalFirst`, cycle guard, budget splitting.

**Exit:** a multi-`sorry` file closed end to end with a materialized artifact that links. Measurable lift over Phase 3 from search alone at equal token budget.

### MVP ships here (10 weeks).

---

## 9. Post-MVP roadmap

### 9.1 Search (6–8 weeks)

`BestFirstDAG`, competing decomposition groups, non-uniform budget allocation, L3 environment snapshots, SGLang benchmark. **Exit:** Putnam 2025, then a paper-level formalization; NFR-3 demonstrated by incremental re-verification of a 10⁴-declaration development costing time proportional to the changed sub-DAG.

### 9.2 Scale and multi-tenancy (4–6 weeks)

Ray actor hosting, **Mathlib zygote with copy-on-write forks**, MCP wrappers for tool packages, gVisor, RLS, quotas, fair queueing, full observability.

The zygote is the load-bearing item. A prelude-bearing worker occupies a full 12 GiB slot, so preludes remove re-elaboration cost but not memory cost — which was the actual constraint. Forking from a parent holding bare Mathlib costs the prelude delta instead: taking Mathlib as ~10.5 GiB of 12 and a delta of ~0.4 GiB **[measure]**, a 256 GiB node supports on the order of 600 base environments rather than five. Caveat: `fork()` in Lean's multi-threaded runtime requires forking before worker threads spawn (the Android zygote pattern), and copy-on-write savings decay as the child writes.

**Exit:** an adversarial `.lean` file attempting elaboration-time IO, network access, and fork-bombing is contained and classified `infra_error`, with no effect on other tenants beyond queueing; quotas hold under a synthetic 20-tenant load.

### 9.3 Training (open-ended)

verl or OpenRLHF against the harness as an RLVR environment. Verifier is the reward; the provenance-filtered trajectory store is the dataset.

Rollout latency is dominated by Lean verification, not generation: colocate REPL actors and rollout actors in one placement group and warm the base-env cache before the run. Expect more effort keeping the verifier fed than on the RL algorithm.

**Hold reward on multi-kernel agreement, not single-kernel acceptance** — see R18.

---

## 10. Risk register

| # | Risk | Position |
|---|---|---|
| **R18** | **An optimizing prover attacks the kernel, not the theorem** | The most serious risk here. A 2026 soundness hunt found a frontier model proving `False` by exploiting reference-counting and GMP bugs in the Lean kernel. An RLVR loop rewarded for "the verifier said yes" is exactly the pressure that finds such bugs, without intent to cheat. Mitigations, none sufficient alone: `lake check --paranoid` on every accepted result; kernel disagreement escalated as a **security incident** with the trajectory preserved, not retried as flaky; admission signals kept running, since a proof of a false statement is the observable symptom; RL reward on multi-kernel agreement. Monitorable, not closable. |
| R19 | Zygote fork may not be viable in Lean's threaded runtime | The multi-tenancy capacity argument rests on it. **[measure]** in Phase 1. If it fails, deployment shifts from shared nodes to per-tenant nodes. |
| R11 | Curated import set too restrictive for project-local libraries | Open, and dependent on R19. |
| R13 | Context construction dominates performance and is specified only structurally | The bands are a starting point, not a result. Phase 3's response cache makes A/B testing cheap. Most-likely-to-change section in this document. |
| R15 | Replay cost unbounded for computation-heavy proofs | Replay has its own timeout; a timeout is not an acceptance. Accept that some legitimate `decide`-heavy proofs become unverifiable within budget. |
| R16 | TCB includes `leanserv`'s verdict-writing path | Reduced, not eliminated. Keep it minimal, review as security-sensitive, consider signing verdict rows. |
| R17 | `.olean` trust base is unverified by replay | Inherent — `.olean` loading does no kernel checking. Read-only mount plus image digest. State the assumption rather than engineering around it. |
| R12 | Client-side tool-call parsing diverges per model family | Conformance suite against server-side behavior on a fixed corpus, per family per release. |
| R1 | Loss of the incumbent harness's context management | Expect a capability dip through Phases 3–4. Port prompts verbatim; keep the Phase 2 symbolic baseline running as the discriminator. |
| R8 | Postgres as queue becomes the bottleneck | Measured non-binding: 0.6–100 claims/s depending on attempt duration, orders below any plausible ceiling. Fix if needed is sharding, not a broker. |

### Deliberately excluded

A web UI beyond the trajectory viewer. A plugin marketplace (MCP has one). An abstraction over proof assistants — the Lean-only assumption is load-bearing in `leankernel`, and Rocq support would be a second `leankernel` and `leanserv`, not a generalized interface. A distributed scheduler before one node saturates. Learned value functions before instrumentation says the heuristic is the bottleneck.

---

## Appendix A — Protocols

```python
class LeanService(Protocol):
    async def check(self, req: CheckRequest) -> CheckResult: ...
    async def check_batch(self, base_env: str, bodies: Sequence[str],
                          opts: CheckOptions) -> list[CheckResult]: ...
    async def seal(self, base_env: str, goals: Sequence[GoalSource]) -> SealedBundle: ...
    async def link(self, req: LinkRequest) -> LinkResponse: ...
    async def decompose(self, base_env: str, body: str) -> Decomposition: ...
    async def materialize_base_env(self, recipe: BaseEnvRecipe) -> BaseEnvRef: ...

class ModelBackend(Protocol):
    id: str
    provenance: ProvenanceClass
    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...
    async def tokenize(self, text: str) -> list[int]: ...

class BlobStore(Protocol):
    async def put(self, data: bytes, media_type: str) -> bytes: ...
    async def get(self, digest: bytes) -> bytes: ...
    async def exists(self, digest: bytes) -> bool: ...
    def url(self, digest: bytes) -> str: ...

class Sink(Protocol):
    async def on_verdict(self, o: Obligation, v: Verdict) -> None: ...
```

## Appendix B — Configuration

```toml
[base_env]
alias   = "mathlib-stable"
imports = ["Mathlib"]
options = { maxRecDepth = 4096 }

[models.prover]
backend = "vllm"
endpoint = "http://vllm:8000"
model_id = "Goedel-LM/Goedel-Prover-V2-8B"
tokenizer_revision = "…"
sampling = { temperature = 0.8, top_p = 0.95, max_tokens = 40960, n = 8 }
context_tokens = 40960           # the served --max-model-len; each request's max_tokens is
                                 # capped to what its prompt leaves of it (the provers' whole
                                 # 40,960-token window, as Goedel-Prover-V2's own pipeline runs)
# request_timeout_s: derived from max_tokens when unset (600 s + 4 tok/s decode)

[models.informal]
backend = "vllm"
model_id = "…"

[leanserv]
memory_cap_gib = 12
warm_per_base_env = 4
command_timeout_ms = 300000
max_heartbeats = 400000
paranoid_replay = false          # true before publishing

[budget]
attempts = 8
tokens = 1000000
kernel_ms = 600000

[policy]
default = "DecomposeAndConquer"
max_depth = 6
```

## Appendix C — Effort

| Phase | Duration | Cumulative |
|---|---|---|
| 0 Foundations | 1 week | 1 |
| 1 Acceptance path | 4 weeks | 5 |
| 2 Null agent | 2 weeks | 7 |
| 3 Model as policy | 2 weeks | 9 |
| 4 Decomposition and tools | 1 week | 10 — **MVP** |
| 5 Search | 6–8 weeks | 18 |
| 6 Scale and multi-tenancy | 4–6 weeks | 24 |
| 7 Training | open-ended | — |

Phase 1 is the schedule risk: `abstractSorry`'s instance-argument and universe-metavariable cases only surface on real Mathlib-dependent goals. Phase 5 carries genuine research risk and its estimate is a floor — "reproduce Putnam 2025" is a research outcome, not an engineering deliverable. Read the totals as time to a system *ready* to do the research, not one that has done it.
