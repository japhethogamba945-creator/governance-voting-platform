"""Explicit, deny-by-default table privileges for the application database roles.

After every ``migrate`` the grants for the application roles are reset to exactly
this manifest. Every table in the ``public`` schema must appear here, even with
no grants, so that adding a table forces a conscious access decision
(enforced by ``tests/test_grants.py``).

Roles (see docs/design/02-architecture.md §3):
  app_rw        web application
  app_voting    ballot casting (from increment 6.6)
  app_tally     counting (from increment 6.7)
  app_audit     audit chaining job (from increment 6.8)
  app_platform  Platform Owner operations
"""

from django.db import connections
from psycopg import sql

APP_ROLES = ("app_rw", "app_voting", "app_tally", "app_audit", "app_platform")

RW = "SELECT, INSERT, UPDATE, DELETE"

GRANTS: dict[str, dict[str, str]] = {
    # --- Django / third-party framework tables ---------------------------
    "django_migrations": {"app_rw": "SELECT"},
    "django_content_type": {"app_rw": "SELECT"},
    "auth_permission": {},
    "auth_group": {},
    "auth_group_permissions": {},
    "django_session": {"app_rw": RW},
    "otp_totp_totpdevice": {"app_rw": RW},
    # --- accounts ----------------------------------------------------------
    "app_user": {
        "app_rw": 'SELECT, INSERT, '
                  'UPDATE (password, last_login, full_name, status, email_verified_at)',
        "app_platform": "SELECT, INSERT",
    },
    "accounts_recovery_code_device": {"app_rw": RW},
    "accounts_recovery_code": {"app_rw": RW},
    "accounts_throttle": {"app_rw": RW},
    # --- tenancy -----------------------------------------------------------
    "organisation": {
        "app_rw": "SELECT, UPDATE (legal_name, timezone, governance_settings)",
        "app_platform": "SELECT, INSERT, UPDATE (status)",
    },
    "org_membership": {"app_rw": "SELECT, INSERT", "app_platform": "SELECT, INSERT"},
    "role_assignment": {
        "app_rw": "SELECT, INSERT, UPDATE (valid_to)",
        "app_platform": "SELECT, INSERT",
    },
    # --- members -----------------------------------------------------------
    "member": {"app_rw": "SELECT, INSERT, UPDATE"},
    "invitation": {"app_rw": "SELECT, INSERT, UPDATE (accepted_at, revoked_at)"},
    "member_import_batch": {"app_rw": "SELECT, INSERT, UPDATE (status, applied_at, rows)"},
    # --- elections -----------------------------------------------------------
    "meeting": {"app_rw": "SELECT, INSERT, UPDATE"},
    # app_voting: UPDATE (id) only so that SELECT ... FOR SHARE is permitted (the
    # guard trigger forbids changing the id); it never updates elections.
    "election": {"app_rw": "SELECT, INSERT, UPDATE", "app_voting": "SELECT, UPDATE (id)"},
    "election_rule_version": {"app_rw": "SELECT, INSERT, UPDATE (approved_by_id, approved_at)",
                              "app_voting": "SELECT"},
    "allowed_transition": {"app_rw": "SELECT"},
    # Written only by the election_record_transition() trigger (SECURITY DEFINER).
    "lifecycle_transition": {"app_rw": "SELECT"},
    # --- nominations ---------------------------------------------------------
    "nomination": {"app_rw": "SELECT, INSERT, UPDATE"},
    "nomination_document": {"app_rw": "SELECT, INSERT, DELETE, UPDATE (scan_status)"},
    "candidate": {"app_rw": "SELECT, INSERT, UPDATE, DELETE", "app_voting": "SELECT"},
    # --- keys and electorate -------------------------------------------------
    "keyholder_key": {"app_rw": "SELECT, INSERT, "
                                "UPDATE (salt, encrypted_private_key, kdf, retired_at)"},
    "key_share": {"app_rw": "SELECT, INSERT"},
    "electorate_entry": {"app_rw": "SELECT, INSERT"},
    # app_rw may revoke; only app_voting can consume (it alone may set the
    # consumed_on/idempotency_hash columns the consumed state requires).
    "entitlement": {"app_rw": "SELECT, INSERT, UPDATE (status, revoked_reason, revoked_by_id)",
                    "app_voting": "SELECT, UPDATE (status, consumed_on, idempotency_hash)"},
    # --- the ballot box (increment 6.6) ---------------------------------------
    # Insert-only for app_voting; the web role has no access at all. app_tally
    # (counting, 6.7) will get SELECT.
    "ballot": {"app_voting": "INSERT"},
    # --- audit -------------------------------------------------------------
    "audit_event": {
        "app_rw": "SELECT, INSERT",
        "app_voting": "INSERT",
        "app_platform": "SELECT, INSERT",
        "app_audit": "SELECT, UPDATE (seq, prev_hash, hash)",
    },
}


def missing_roles(using: str = "default") -> list[str]:
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", [list(APP_ROLES)])
        present = {row[0] for row in cursor.fetchall()}
    return [r for r in APP_ROLES if r not in present]


def apply_grants(using: str = "default") -> None:
    """Reset the application roles' privileges to the manifest."""
    roles = sql.SQL(", ").join(sql.Identifier(r) for r in APP_ROLES)
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = [row[0] for row in cursor.fetchall()]
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(roles))
        for table in tables:
            ident = sql.Identifier(table)
            cursor.execute(sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(ident, roles))
            for role, privileges in GRANTS.get(table, {}).items():
                # Privilege strings come from this module only, never from input.
                cursor.execute(
                    sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                        sql.SQL(privileges), ident, sql.Identifier(role)
                    )
                )
