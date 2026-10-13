# Governance Voting Platform

A multi-organisation voting and governance platform for clubs and membership organisations: membership registers, nominations, secret-ballot remote elections, counting, certification and audit.

> **Status: prototype, increments 6.1–6.2 (foundation; organisation, membership and invitations).** Not for real elections. Development uses fictional data only. The design and approval history live in [`docs/design/`](docs/design/README.md).

## Increment 6.2: organisation, membership and invitations

| Area | Implemented |
|---|---|
| Membership register | Add, edit, search/filter, deactivate (step-up + mandatory reason) and reactivate members. Unique member number and email per organisation. Eligibility-relevant changes (category, subscription, joined date, status) are audited with old and new values; other personal fields by name only |
| CSV import | Upload → preview (create / update / unchanged / error per row) → apply with step-up. All or nothing; never deactivates; refused if the register changed since the preview |
| Invitations | Single-use, 7-day token (only its SHA-256 is stored), resend revokes the previous link, token moved out of the URL immediately and redacted from all logs; acceptance sets a password and goes straight into authenticator enrolment. Inviting an email that already has an account links it without revealing that to the administrator |
| Roles | People-and-roles page; org admins grant/revoke Returning Officer, Auditor and Org Admin (step-up, audited); cannot change own roles; the last administrator cannot be removed (locked against concurrent revocation). The Member role follows the register |
| Access | A membership with no active role now has no access (same 404 as a non-member) |
| Row-level security on `app_user` | Users are visible only to themselves, to organisations they belong to, and transiently to a sign-in lookup by exact email or an invitation lookup by token. Resolves decision-log item G5.5 |
| Tests | 129, including an extended cross-tenant matrix that also checks every object URL against another organisation's object IDs |

## What increment 6.1 contains

| Area | Implemented |
|---|---|
| Project skeleton | Django 5.2 LTS, PostgreSQL 16, server-rendered templates, no inline scripts (strict CSP) |
| Database roles | `db/roles.sql`; deny-by-default grants manifest (`platform_core/grants.py`) re-applied after every migration |
| Tenant isolation | `organisation_id` on every tenant table, forced Row-Level Security, composite foreign keys, `TenantManager` app-level scoping, organisation resolved from URL + membership (404 for non-members) |
| Authentication | Email + password (Argon2id) + mandatory TOTP; hashed single-use recovery codes; account and IP throttling with no account enumeration; session rotation, idle/absolute timeouts (stricter for officials); step-up decorator for privileged actions |
| RBAC | Role → permission table (`tenancy/roles.py`), `@requires(Perm...)` view decorator |
| Audit (minimal) | Append-only `audit_event` (trigger blocks update/delete/truncate; only the future chaining role may fill hash fields once); ballot-like keys rejected from details |
| Platform Owner | `provision_organisation` command on a separate `app_platform` connection |
| Tests | 58 tests against real PostgreSQL running as `app_rw`: RLS catalogue guard, grants-manifest guard, DB-level and HTTP-level cross-tenant matrix (auto-discovers every `/o/<org_slug>/` route), auth, permissions, headers, audit immutability |

Next increments are listed in [`docs/design/BLUEPRINT.md` §9](docs/design/BLUEPRINT.md#9-phased-implementation-plan-with-acceptance-gates).

## Local development

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL 16 (or Docker).

```sh
# 1. PostgreSQL: either `docker compose up db`, or on a local cluster:
psql -U postgres -c "CREATE ROLE gvp_admin LOGIN SUPERUSER PASSWORD 'gvp_admin'"   # dev/test only
psql -U postgres -c "CREATE DATABASE gvp OWNER gvp_admin"
psql -U postgres -d gvp -f db/roles.sql

# 2. Python
uv sync
cp .env.example .env && set -a && . ./.env && set +a

# 3. Migrate as the admin login (no assumed role), then run as app_rw
DB_ASSUME_ROLE= uv run python manage.py migrate
uv run python manage.py provision_organisation --slug acacia-club \
    --name "Acacia Gardens Club (fictional)" --admin-email chair@acacia.example
uv run python manage.py dev_activate_user chair@acacia.example 'a long dev passphrase'
uv run python manage.py runserver
```

Sign in at <http://localhost:8000/accounts/login/>; first sign-in enrols an authenticator app.

### Tests

```sh
uv run pytest            # needs the gvp_admin login and db/roles.sql applied
uv run ruff check .
```

The test runner migrates as the admin login, then switches the connection to `app_rw` so the whole suite runs under production privileges and RLS (`tests/conftest.py`).

## Production notes

- Each process logs in directly as its own role (`app_rw`, `app_platform`, ...); never as an administrator, and without `DB_ASSUME_ROLE`.
- Migrations run as `app_owner` in a separate release step.
- Hosting: a Kenyan data centre by default for real personal data (decision D4, pending legal review).
- See `docs/design/05-security-review.md` before any deployment.
