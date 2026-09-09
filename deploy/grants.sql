-- Privilege model (spec §5.5). Enforcement, not documentation: tested against PostgreSQL 16,
-- where a table-level GRANT UPDATE confers update on every column and a later column-level
-- REVOKE does NOT subtract from it. Every grant below must therefore be written narrow from the
-- start (grant-only), never as a broad grant followed by a revoke.
--
-- Phase 0 creates the two roles only. The GRANT/REVOKE statements against actual tables
-- (obligation, attempt, verdict, verification_cache, base_env, blob, mark_proved) cannot be
-- written correctly until Phase 1's DDL lands — see spec §5.3 for the schema and §5.5 for the
-- exact grants to apply once it exists. Do not guess at grants against tables that don't exist.

CREATE ROLE app LOGIN;
CREATE ROLE leanserv LOGIN;

-- Phase 1 adds, per spec §5.5:
--
-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO app;
-- GRANT INSERT ON attempt, tool_call, trajectory, obligation, obligation_edge TO app;
-- GRANT UPDATE (priority, spent_attempts, spent_tokens, spent_kernel_ms, updated_at)
--   ON obligation TO app;                       -- never GRANT UPDATE ON obligation
-- GRANT UPDATE ON attempt TO app;
-- REVOKE INSERT ON verdict FROM app;             -- table-level revoke DOES work
-- GRANT EXECUTE ON FUNCTION mark_proved TO app;
--
-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO leanserv;
-- GRANT INSERT ON verdict TO leanserv;
-- GRANT INSERT, UPDATE ON verification_cache, base_env, blob TO leanserv;
--
-- plus the `mark_proved` SECURITY DEFINER function itself (spec §5.5), which is the only path to
-- `proved` status and must set `SET search_path = pg_catalog, public` — omitting that is an
-- escalation vector.
