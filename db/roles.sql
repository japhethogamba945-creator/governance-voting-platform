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
