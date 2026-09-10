-- Privilege model (spec §5.5). Enforcement, not documentation: tested against PostgreSQL 16,
-- where a table-level GRANT UPDATE confers update on every column and a later column-level
-- REVOKE does NOT subtract from it. Every grant below is therefore written narrow from the
-- start (grant-only), never as a broad grant followed by a revoke.
--
-- Idempotent: safe to re-run against a database that already has these roles/grants (uses
-- `IF NOT EXISTS` for role creation and IF-wrapped checks where Postgres has no direct
-- equivalent), since deploys are expected to apply it every time, not just once.
--
-- One caveat with teeth: `CREATE OR REPLACE FUNCTION` cannot change an existing function's return
-- type ("cannot change return type of existing function"). Changing one -- as M2.3 did, turning
-- `claim_attempt` from `RETURNS uuid` into `RETURNS TABLE(...)` -- needs an explicit
-- `DROP FUNCTION` against every database that already has the old signature. Adding a *new*
-- parameter is likewise a new overload rather than a replacement, leaving the old one callable.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app') THEN
    CREATE ROLE app LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'leanserv') THEN
    CREATE ROLE leanserv LOGIN;
  END IF;
END
$$;

-- Application (API + agent workers).
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app;
GRANT INSERT ON attempt, tool_call, trajectory, obligation, obligation_edge TO app;
-- `run` is not in spec §5.5's own list, which is an omission rather than a deliberate exclusion:
-- §6.1 puts `POST /v1/runs` ("create a run from a submission") in the *public* API, which is the
-- `app` role, so ingestion cannot create the run it is asked to create without this. Confirmed
-- the hard way -- ingestion's first run against the real grants failed with "permission denied
-- for table run".
--
-- INSERT only, not UPDATE. Nothing in M2.6 changes a run after creation, and §6.1's cancel
-- endpoint (M2.8) is the thing that will need `UPDATE (status)` -- granted then, with its own
-- reason, rather than pre-emptively here. `run.status` gates `claim_attempt`, so widening it is a
-- scheduling decision and not merely bookkeeping.
GRANT INSERT ON run TO app;
GRANT UPDATE (priority, spent_attempts, spent_tokens, spent_kernel_ms, updated_at)
  ON obligation TO app;                       -- never GRANT UPDATE ON obligation
GRANT UPDATE ON attempt TO app;
REVOKE INSERT ON verdict FROM app;            -- table-level revoke DOES work
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO app;

-- Lean Execution Service: the only writer of verdicts.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO leanserv;
GRANT INSERT ON verdict TO leanserv;
GRANT INSERT, UPDATE ON verification_cache, base_env, blob TO leanserv;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO leanserv;

-- The only path to `proved` status. Workers request a check (via leanserv, which writes the
-- verdict row) and observe the outcome through this function; they never transcribe it directly
-- by updating `obligation.status` themselves -- and `app`'s own grants above make that
-- structurally impossible regardless, since `status` isn't in app's permitted-column list.
--
-- SECURITY DEFINER runs this with the *owner's* privileges, not the caller's -- otherwise `app`
-- (which cannot UPDATE obligation.status directly) couldn't call it either. `SET search_path`
-- is mandatory for any SECURITY DEFINER function: without it, a caller who can control
-- `search_path` in their own session can shadow `pg_catalog`/`public` objects this function
-- resolves unqualified, redirecting what it actually executes -- the classic SECURITY DEFINER
-- privilege-escalation vector.
CREATE OR REPLACE FUNCTION mark_proved(p_obligation uuid, p_attempt uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
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

GRANT EXECUTE ON FUNCTION mark_proved TO app;

-- ---------------------------------------------------------------------------
-- Obligation state machine (spec §6.4). M2.2.
--
-- Every status transition is a SECURITY DEFINER function, not a plain UPDATE, for the same
-- reason `mark_proved` above is one: `app` has no UPDATE privilege on `obligation.status` at
-- all (confirmed empirically -- a direct `UPDATE obligation SET status = ...` as `app` fails
-- with "permission denied for table obligation"), so there is no other way for the application
-- to move an obligation through its lifecycle.
--
-- This resolves a genuine tension in the spec. §6.4's claim SQL is written as a bare
-- `UPDATE obligation o SET status = 'in_progress'`, which cannot run as `app` under §5.5's own
-- grant list. §5.5 is the more precise statement of intent ("never GRANT UPDATE ON obligation"),
-- so the transitions become functions rather than the grants becoming broader. The alternative --
-- granting UPDATE(status) and adding a trigger that rejects the single value 'proved' -- was
-- rejected: it is a deny-list over values, and this codebase's rule is allowlist-never-deny-list
-- wherever a check decides whether something is trusted.
--
-- Each function re-derives its own precondition from committed rows rather than trusting its
-- caller, which is what makes these enforcement rather than convenience. A worker asks for a
-- transition and observes whether it happened; it never asserts that one is warranted.

-- `in_progress` -> `decomposed`. Requires the decomposition group to actually exist: spec's own
-- gloss on this status is "children exist", and a parent parked in `decomposed` with no children
-- is unschedulable and unprovable -- it would never be claimed again and never have a group to
-- reassemble, i.e. silently abandoned.
CREATE OR REPLACE FUNCTION mark_decomposed(p_obligation uuid, p_group uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM obligation_edge e
    WHERE e.parent_id = p_obligation AND e.group_id = p_group
  ) THEN
    RAISE EXCEPTION 'decomposition group % of obligation % has no children', p_group, p_obligation;
  END IF;

  UPDATE obligation o SET status = 'decomposed', updated_at = now()
  WHERE o.id = p_obligation AND o.status IN ('open', 'in_progress');
  IF FOUND THEN RETURN; END IF;

  -- Competing decomposition groups are normal (spec §4.5: "a parent may hold several competing
  -- decompositions"), so a second group arriving for an already-decomposed parent is a no-op,
  -- not an error. Same shape as `mark_proved`'s idempotence for concurrent attempts.
  IF EXISTS (SELECT 1 FROM obligation WHERE id = p_obligation AND status = 'decomposed') THEN
    RETURN;
  END IF;
  RAISE EXCEPTION 'obligation % is not in a decomposable status', p_obligation;
END $$;

-- `in_progress` -> `open`, spec §6.4's "errors / timeout" and "infra_error" arrows. They differ
-- only in whether the attempt is charged, which is exactly why this takes `p_charge` instead of
-- being two functions: `infra_error` is a first-class *unbudgeted* outcome, and charging it would
-- make infrastructure trouble consume an obligation's proof budget.
--
-- The charge happens in the same statement as the status change deliberately. `spent_attempts` is
-- in `app`'s own permitted-column list, so a caller could increment it separately -- but then the
-- obligation is briefly `open` with the attempt uncharged, and the scheduler can re-claim it in
-- that window, spending more attempts than the budget allows.
CREATE OR REPLACE FUNCTION release_obligation(p_obligation uuid, p_charge boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  UPDATE obligation o
  SET status = 'open',
      spent_attempts = o.spent_attempts + (CASE WHEN p_charge THEN 1 ELSE 0 END),
      updated_at = now()
  WHERE o.id = p_obligation AND o.status = 'in_progress';
  IF FOUND THEN RETURN; END IF;

  -- Another attempt on the same obligation may have already proved or decomposed it while this
  -- one was running; releasing then must not drag it back to `open`. Terminal and decomposed
  -- statuses win over a late release, silently, because there is nothing wrong with the race.
  IF EXISTS (
    SELECT 1 FROM obligation
    WHERE id = p_obligation AND status IN ('proved', 'decomposed', 'failed', 'blocked', 'abandoned')
  ) THEN
    RETURN;
  END IF;
  RAISE EXCEPTION 'obligation % is not in progress', p_obligation;
END $$;

-- `in_progress` -> `failed`, spec §6.4's "budget exhausted" arrow. The exhaustion test is made
-- here, from the committed row, rather than taken on the caller's word: a caller that could
-- declare an obligation failed at will could retire work that still has budget, which is a
-- capability nothing in the design needs.
CREATE OR REPLACE FUNCTION mark_failed(p_obligation uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  UPDATE obligation o SET status = 'failed', updated_at = now()
  WHERE o.id = p_obligation
    AND o.status IN ('open', 'in_progress')
    AND o.spent_attempts >= o.budget_attempts;
  IF FOUND THEN RETURN; END IF;

  IF EXISTS (SELECT 1 FROM obligation WHERE id = p_obligation AND status = 'failed') THEN
    RETURN;
  END IF;
  RAISE EXCEPTION 'obligation % has budget remaining or is not failable', p_obligation;
END $$;

-- `decomposed` -> `blocked`, spec §6.4's "every group has a failed child" arrow. Like
-- `mark_failed`, the predicate is computed here from committed rows.
--
-- "Every group" is load-bearing and is why this is not simply "a child failed": competing
-- decomposition groups exist precisely so that one group's dead end does not kill the parent
-- (spec §4.5: "a failed reassembly fails only that decomposition group, and proved children
-- remain reusable by a competing group"). A parent is blocked only when no group can still
-- succeed. A parent with no groups at all is not blocked -- it is simply not decomposed.
CREATE OR REPLACE FUNCTION mark_blocked(p_obligation uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  v_groups int;
  v_dead_groups int;
BEGIN
  SELECT count(DISTINCT e.group_id) INTO v_groups
  FROM obligation_edge e WHERE e.parent_id = p_obligation;

  SELECT count(*) INTO v_dead_groups FROM (
    SELECT e.group_id
    FROM obligation_edge e
    JOIN obligation c ON c.id = e.child_id
    WHERE e.parent_id = p_obligation
    GROUP BY e.group_id
    HAVING bool_or(c.status IN ('failed', 'blocked', 'abandoned'))
  ) dead;

  IF v_groups = 0 OR v_dead_groups < v_groups THEN
    RAISE EXCEPTION 'obligation % still has a decomposition group that can succeed', p_obligation;
  END IF;

  UPDATE obligation o SET status = 'blocked', updated_at = now()
  WHERE o.id = p_obligation AND o.status = 'decomposed';
  IF FOUND THEN RETURN; END IF;

  IF EXISTS (SELECT 1 FROM obligation WHERE id = p_obligation AND status = 'blocked') THEN
    RETURN;
  END IF;
  RAISE EXCEPTION 'obligation % is not decomposed', p_obligation;
END $$;

-- Spec §5.3's cycle guard: "A cycle guard runs in the same transaction that inserts an edge -- a
-- child's `goal_digest` may not equal any ancestor's, and `depth` is capped by `run.max_depth`.
-- Without it, a policy can decompose an obligation into itself and consume budget forever."
--
-- A BEFORE INSERT trigger, not a check the application performs, and not a deferred constraint:
-- "in the same transaction that inserts an edge" is satisfied by construction here, and `app`
-- holds INSERT on `obligation_edge` directly, so any application-side check would be advisory.
--
-- The digest test catches a cycle spelled the same way twice; `run.max_depth` is the
-- unconditional backstop for one spelled two different ways, which is why both exist (see
-- `lean_agent_core.digests` on why `goal_digest` is sound but incomplete for this).
CREATE OR REPLACE FUNCTION obligation_edge_cycle_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
  v_child_digest bytea;
  v_child_depth int;
  v_max_depth int;
BEGIN
  IF NEW.parent_id = NEW.child_id THEN
    RAISE EXCEPTION 'obligation % cannot be its own child', NEW.parent_id;
  END IF;

  SELECT o.goal_digest, o.depth, r.max_depth INTO v_child_digest, v_child_depth, v_max_depth
  FROM obligation o JOIN run r ON r.id = o.run_id
  WHERE o.id = NEW.child_id;

  IF v_child_depth > v_max_depth THEN
    RAISE EXCEPTION 'child % is at depth %, past run max_depth %',
      NEW.child_id, v_child_depth, v_max_depth;
  END IF;

  IF EXISTS (
    WITH RECURSIVE ancestor(id, goal_digest) AS (
      SELECT o.id, o.goal_digest FROM obligation o WHERE o.id = NEW.parent_id
      UNION
      SELECT o.id, o.goal_digest
      FROM obligation_edge e
      JOIN ancestor a ON e.child_id = a.id
      JOIN obligation o ON o.id = e.parent_id
    )
    SELECT 1 FROM ancestor WHERE ancestor.goal_digest = v_child_digest
  ) THEN
    RAISE EXCEPTION 'child % repeats an ancestor''s goal_digest -- decomposition cycle',
      NEW.child_id;
  END IF;

  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS obligation_edge_cycle_guard ON obligation_edge;
CREATE TRIGGER obligation_edge_cycle_guard
  BEFORE INSERT ON obligation_edge
  FOR EACH ROW EXECUTE FUNCTION obligation_edge_cycle_guard();

GRANT EXECUTE ON FUNCTION mark_decomposed TO app;
GRANT EXECUTE ON FUNCTION release_obligation TO app;
GRANT EXECUTE ON FUNCTION mark_failed TO app;
GRANT EXECUTE ON FUNCTION mark_blocked TO app;

-- ---------------------------------------------------------------------------
-- Scheduler (spec §6.4). M2.3.

-- `open` -> `in_progress`, plus the `attempt` row that owns the lease. Spec §6.4 writes this as a
-- CTE chain in the application; it is a function here for M2.2's reason -- `app` cannot write
-- `obligation.status` -- and the whole chain has to be one statement anyway, since a claim that
-- selected an obligation in one round trip and marked it in another would hand the same work to
-- two workers.
--
-- Returns the new attempt's id, or NULL when nothing is claimable. NULL is the ordinary idle case
-- (spec's control loop backs off on it), not an error.
--
-- `FOR UPDATE OF o SKIP LOCKED` is what makes concurrent workers pick *different* obligations
-- rather than serializing on the highest-priority row. Spec names the cost and accepts it:
-- "ORDER BY … SKIP LOCKED can invert priority under contention; documented, and shardable by
-- hashtext(id) if it becomes visible."
--
-- `p_tenants` is spec's `$eligible_tenants`, "refreshed by the admission loop". No admission loop
-- exists yet (multi-tenancy is post-MVP, §9), so NULL means "no tenant filter" -- an explicit
-- pass-through rather than a hardcoded assumption that every tenant is eligible, so the parameter
-- is already in place when the loop that computes it arrives.
--
-- The budget test here is `spent_attempts < budget_attempts`, checked again exactly at commit per
-- spec: "Quota is checked coarsely here and exactly at commit; over-admission by one attempt is
-- acceptable, a per-row function call on a locking scan is not."
-- Returns a one-row table rather than a bare uuid, so the caller reads the attempt in the same
-- round trip. A `uuid`-returning version, called as `SELECT ... FROM attempt a WHERE a.id =
-- claim_attempt(...)`, is a trap and was written first: the function is VOLATILE, so Postgres
-- evaluates it *once per scanned row* of `attempt`, claiming a fresh obligation every time.
CREATE OR REPLACE FUNCTION claim_attempt(
  p_worker   text,
  p_lease    interval,
  p_policy   text,
  p_cfg_hash bytea,
  p_tenants  uuid[] DEFAULT NULL
) RETURNS TABLE (attempt_id uuid, obligation_id uuid, run_id uuid)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  v_obligation uuid;
  v_run        uuid;
  v_attempt    uuid;
BEGIN
  SELECT o.id, o.run_id INTO v_obligation, v_run
  FROM obligation o
  JOIN run r ON r.id = o.run_id
  WHERE o.status = 'open'
    AND o.spent_attempts < o.budget_attempts
    AND r.status = 'running'
    AND (p_tenants IS NULL OR r.tenant_id = ANY(p_tenants))
  ORDER BY o.priority DESC, o.depth ASC, o.created_at ASC
  FOR UPDATE OF o SKIP LOCKED
  LIMIT 1;

  IF v_obligation IS NULL THEN
    RETURN;  -- no rows: the ordinary idle case
  END IF;

  UPDATE obligation SET status = 'in_progress', updated_at = now() WHERE id = v_obligation;

  INSERT INTO attempt (obligation_id, run_id, policy_id, policy_config_hash,
                       lease_owner, lease_expires_at, heartbeat_at, status)
  VALUES (v_obligation, v_run, p_policy, p_cfg_hash,
          p_worker, now() + p_lease, now(), 'claimed')
  RETURNING id INTO v_attempt;

  RETURN QUERY SELECT v_attempt, v_obligation, v_run;
END $$;

-- Reap attempts whose lease has expired: the worker holding them is gone.
--
-- Spec is explicit about what this means and it is worth restating where the code is: "Lease
-- expiry means *the worker is gone*, never *the attempt took too long*. Wallclock is a budget
-- concern." So the obligation is returned to `open` **without charging an attempt** -- charging
-- would let a node that keeps dying quietly consume every obligation's budget and depress the
-- reported pass rate, which is the same failure `infra_error` exists to prevent.
--
-- Both writes happen in one statement pair inside one function deliberately. Expiring the attempt
-- and releasing the obligation separately leaves a window -- and a crash in that window leaves an
-- obligation stuck `in_progress` with no live attempt, which nothing would ever reclaim.
--
-- The obligation update is written inline rather than calling `release_obligation`: that function
-- raises when the obligation is not releasable, which is correct for a single worker reporting its
-- own outcome and wrong for a best-effort batch sweep that must not abort on one odd row.
CREATE OR REPLACE FUNCTION reap_expired_attempts(p_limit int DEFAULT 100)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  v_reaped int;
BEGIN
  WITH expired AS (
    SELECT a.id, a.obligation_id
    FROM attempt a
    WHERE a.status IN ('claimed', 'running')
      AND a.lease_expires_at IS NOT NULL
      AND a.lease_expires_at < now()
    ORDER BY a.lease_expires_at
    FOR UPDATE OF a SKIP LOCKED
    LIMIT p_limit
  ),
  closed AS (
    UPDATE attempt a SET status = 'expired', finished_at = now(), lease_owner = NULL
    FROM expired WHERE a.id = expired.id
    RETURNING a.obligation_id
  ),
  released AS (
    UPDATE obligation o SET status = 'open', updated_at = now()
    FROM closed WHERE o.id = closed.obligation_id AND o.status = 'in_progress'
    RETURNING o.id
  )
  SELECT count(*) INTO v_reaped FROM closed;
  RETURN v_reaped;
END $$;

GRANT EXECUTE ON FUNCTION claim_attempt TO app;
GRANT EXECUTE ON FUNCTION reap_expired_attempts TO app;

-- ---------------------------------------------------------------------------
-- Bundle materialization (spec §4.1, §6.3). M2.7.

-- Record the digest of a sealed bundle's compiled `.olean` against every obligation that names
-- that bundle. **Write-once**: it only ever fills a NULL, never changes an existing value.
--
-- Not a plain UPDATE, because `sealed_olean_sha` is not in `app`'s permitted-column list and must
-- not be. `mark_proved` accepts a proof only when `v.sealed_olean_sha_observed = o.sealed_olean_sha`
-- -- an `app` that could write this column could set it to whatever digest a verdict happened to
-- observe, which turns spec's seal-integrity check into a tautology and defeats the one thing it
-- exists to catch (a worker importing a different bundle than the obligation was created against).
--
-- Write-once is what makes that hold over time rather than only at the first write: once an
-- obligation's compiled bundle is named, no later materialization can rename it, so the artifact
-- an obligation is judged against is fixed for its whole life.
--
-- A caller could still lie *here*, passing a digest it did not compute. That is safe in the only
-- direction that matters: leanserv observes the real digest of the file it actually imported
-- (M2.1.2), so a lie makes `mark_proved` refuse every subsequent proof rather than accept a bad
-- one. Lying costs you your own proofs.
--
-- Returns how many obligations it filled. Zero is not an error -- a bundle can be re-materialized
-- after a restart, and finding every obligation already stamped is the normal idempotent outcome.
CREATE OR REPLACE FUNCTION materialize_bundle(p_bundle_sha bytea, p_olean_sha bytea)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  v_filled int;
BEGIN
  IF p_olean_sha IS NULL THEN
    RAISE EXCEPTION 'refusing to materialize bundle % with a NULL olean digest', p_bundle_sha;
  END IF;

  -- A bundle already stamped with a *different* digest means two different compilations of the
  -- same sealed source, which should be impossible: the source is content-addressed, so a digest
  -- mismatch says the build is not reproducible or the bundle root has been tampered with. Loud,
  -- because silently keeping the first value would leave the two facts disagreeing forever.
  IF EXISTS (
    SELECT 1 FROM obligation
    WHERE bundle_sha = p_bundle_sha
      AND sealed_olean_sha IS NOT NULL
      AND sealed_olean_sha <> p_olean_sha
  ) THEN
    RAISE EXCEPTION 'bundle % is already materialized with a different .olean digest', p_bundle_sha;
  END IF;

  UPDATE obligation SET sealed_olean_sha = p_olean_sha, updated_at = now()
  WHERE bundle_sha = p_bundle_sha AND sealed_olean_sha IS NULL;
  GET DIAGNOSTICS v_filled = ROW_COUNT;
  RETURN v_filled;
END $$;

GRANT EXECUTE ON FUNCTION materialize_bundle TO app;
