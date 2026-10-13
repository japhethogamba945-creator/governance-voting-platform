"""Database-catalogue guards: tenant tables must be protected by RLS."""

import pytest
from django.db import connection

from platform_core.db import acting_as

pytestmark = pytest.mark.django_db


def _tenant_tables():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'organisation_id'"
        )
        return sorted(r[0] for r in cursor.fetchall())


def test_every_tenant_table_has_forced_rls_and_tenant_policy():
    tables = _tenant_tables()
    assert {"org_membership", "role_assignment", "audit_event"} <= set(tables)
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = %s::regclass", [table])
            assert cursor.fetchone() == (True, True), f"{table}: RLS not enabled+forced"
            cursor.execute(
                "SELECT 1 FROM pg_policies "
                "WHERE tablename = %s AND policyname = 'tenant_isolation'",
                [table])
            assert cursor.fetchone(), f"{table}: missing tenant_isolation policy"


def test_organisation_table_has_forced_rls():
    with connection.cursor() as cursor:
        cursor.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                       "WHERE oid = 'organisation'::regclass")
        assert cursor.fetchone() == (True, True)


@pytest.mark.parametrize("role", ["app_rw", "app_voting", "app_tally", "app_audit",
                                  "app_platform", "app_owner"])
def test_application_roles_are_not_privileged(role):
    with acting_as(None), connection.cursor() as cursor:
        cursor.execute("SELECT rolsuper, rolbypassrls, rolcreaterole FROM pg_roles "
                       "WHERE rolname = %s", [role])
        assert cursor.fetchone() == (False, False, False)


def test_runtime_connection_is_app_rw():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone()[0] == "app_rw"
