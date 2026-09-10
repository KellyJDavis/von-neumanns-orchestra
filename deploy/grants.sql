-- Privilege model (spec §5.5). Enforcement, not documentation: tested against PostgreSQL 16,
-- where a table-level GRANT UPDATE confers update on every column and a later column-level
-- REVOKE does NOT subtract from it. Every grant below is therefore written narrow from the
-- start (grant-only), never as a broad grant followed by a revoke.
--
-- Idempotent: safe to re-run against a database that already has these roles/grants (uses
-- `IF NOT EXISTS` for role creation and IF-wrapped checks where Postgres has no direct
-- equivalent), since deploys are expected to apply it every time, not just once.

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
