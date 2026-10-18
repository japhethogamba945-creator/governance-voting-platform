# Backup, restore and the restore drill

Design: [05 §9](../design/05-security-review.md#9-backup-recovery-and-availability). Acceptance test 16.

> This repository provides the checks and an automated drill. The production backup service (continuous WAL archiving with point-in-time recovery, encrypted base backups in a second facility, synchronous replication while elections are open) is part of the hosting set-up and must be configured and tested on the real infrastructure before the pilot.

## What must be backed up

| Item | Where | Notes |
|---|---|---|
| PostgreSQL database | the `DB_NAME` database, including roles' grants, row-level security policies and triggers | Roles themselves are cluster-level: re-create them with `db/roles.sql` before restoring into a new cluster. |
| Audit anchor files | `AUDIT_ANCHOR_DIR` | Must live in write-once storage of their own (I6.8.3). Back them up separately from the database; they are the evidence that the database's audit trail was not rewritten. |
| Private files | `PRIVATE_MEDIA_ROOT` (nomination documents) | |
| Secrets | `DJANGO_SECRET_KEY`, database passwords, email credentials | In the secrets store, not in backups of the application. Without the same `DJANGO_SECRET_KEY`, idempotency keys and sessions issued before the restore become invalid (votes are not affected). |

The sealed offline **backup code** of each election, and the keyholders' passphrases, are never in any backup by design.

## Restoring

1. If an election is **open**, follow the exceptional procedure in 05 §9 first (suspend; restore; compare receipts reported by voters with the restored commitments; the Returning Officer decides the remedy).
2. Re-create the roles if the cluster is new: `psql -f db/roles.sql`.
3. Restore, for example from a logical backup: `pg_restore --exit-on-error --dbname=<new database> backup.dump`.
4. Point `AUDIT_ANCHOR_DIR` at the (read-only) anchor files.
5. Run the checks, with an operator login that may `SET ROLE app_rw` and `app_platform`:

   ```
   python manage.py post_restore_check
   ```

   It checks that every migration is applied, that the application roles' privileges match `platform_core/grants.py` exactly, that row-level security is forced on every tenant table and no application role can bypass it, that every organisation's audit chain verifies against its anchors and anchor files, and that every closed election's recorded fingerprints (rules, counting key, electorate, ballot box, counts, certified documents, receipt list) are still consistent. It exits with an error if anything fails.
6. Only then point the application at the restored database.

## The restore drill

`tests/test_restore_drill.py` runs in continuous integration on every change. It:

1. runs a fictional election to a published result and chains and anchors the audit trail;
2. backs the database up with `pg_dump --format=custom` and restores it with `pg_restore` into a new database;
3. runs `post_restore_check` on the restored copy (all checks pass);
4. **recounts the restored ballot box** with two keyholders' passphrases, proving the counting keys survive a restore: the recount reproduces the original totals and ballot-box digest exactly;
5. damages the restored audit trail and confirms `post_restore_check` then fails;
6. drops the restored copy and confirms the source database was not changed.

Before the pilot, and every quarter after it, the same drill must also be run by operators against a real backup restored into an isolated environment (05 §9), and the output kept with the drill record.
