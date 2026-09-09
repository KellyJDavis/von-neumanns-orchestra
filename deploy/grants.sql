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
