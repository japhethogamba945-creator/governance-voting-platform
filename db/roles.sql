-- Application database roles (docs/design/02-architecture.md §3).
-- Idempotent. Run once per PostgreSQL cluster as an administrator, before migrations.
--
-- Production: give each runtime role LOGIN and a strong password from the
-- secrets store, e.g.  ALTER ROLE app_rw LOGIN PASSWORD '...';
-- Migrations run as app_owner (schema owner), never as a runtime role.
-- No role here may have SUPERUSER or BYPASSRLS.

DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['app_owner', 'app_rw', 'app_voting', 'app_tally', 'app_audit', 'app_platform']
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE', r);
    END IF;
  END LOOP;
END $$;

-- Ballot casting switches to app_voting only inside the casting transaction
-- (SET LOCAL ROLE). app_rw does not inherit app_voting's privileges.
-- (PostgreSQL 16+ syntax; re-running only emits a notice.)
GRANT app_voting TO app_rw WITH INHERIT FALSE, SET TRUE;

-- Counting switches to app_tally only inside the counting transaction
-- (increment 6.7). app_tally alone can read ballot ciphertexts and write tallies.
GRANT app_tally TO app_rw WITH INHERIT FALSE, SET TRUE;

-- The background job process (manage.py run_scheduler) chains and anchors the
-- audit trail as app_audit (increment 6.8). Production: give the job process
-- its own LOGIN role that is a member of app_rw and app_audit, e.g.
--   CREATE ROLE gvp_jobs LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
--   GRANT app_rw, app_audit TO gvp_jobs;
-- and run it with DB_USER=gvp_jobs DB_ASSUME_ROLE=app_rw. The web role is never
-- granted app_audit.
