"""Acceptance test 1 (database layer): organisations cannot see or write each other's rows."""

import pytest
from django.db import DatabaseError, connection, transaction

from audit.models import AuditEvent
from audit.services import record
from platform_core.db import NoTenantContext, acting_as, db_context
from tenancy.models import Organisation, OrgMembership, RoleAssignment
from tenancy.roles import Role
from tests.factories import add_member, make_user

pytestmark = pytest.mark.django_db


def _count(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()[0]


def test_no_context_sees_nothing(two_orgs):
    with transaction.atomic():
        assert _count("SELECT count(*) FROM org_membership") == 0
        assert _count("SELECT count(*) FROM role_assignment") == 0
        assert _count("SELECT count(*) FROM organisation") == 0


def test_org_context_sees_only_own_rows(two_orgs):
    a, b = two_orgs
    with transaction.atomic(), db_context(org_id=a.org.id):
        assert _count("SELECT count(*) FROM org_membership WHERE organisation_id = %s",
                      [b.org.id]) == 0
        assert _count("SELECT count(*) FROM role_assignment WHERE organisation_id = %s",
                      [b.org.id]) == 0
        assert _count("SELECT count(*) FROM org_membership") == 1
        assert list(Organisation.objects.values_list("slug", flat=True)) == ["acacia-club"]


def test_cannot_insert_into_another_org(two_orgs):
    a, b = two_orgs
    user, _ = make_user("someone@example.org")
    with transaction.atomic(), db_context(org_id=a.org.id):
        with pytest.raises(DatabaseError, match="row-level security"), transaction.atomic():
            OrgMembership.objects.create(organisation=b.org, user=user)


def test_cannot_update_another_orgs_settings(two_orgs):
    a, b = two_orgs
    with transaction.atomic(), db_context(org_id=a.org.id):
        updated = _count(
            "WITH u AS (UPDATE organisation SET legal_name = 'x' WHERE id = %s RETURNING 1) "
            "SELECT count(*) FROM u", [b.org.id])
        assert updated == 0


def test_composite_fk_blocks_cross_org_role_assignment(two_orgs):
    a, b = two_orgs
    with acting_as(None):
        b_membership = OrgMembership.all_objects.get(organisation=b.org)
        with pytest.raises(DatabaseError, match="role_assignment_membership_same_org"), \
                transaction.atomic():
            RoleAssignment.all_objects.create(organisation=a.org, membership=b_membership,
                                              role=Role.ORG_ADMIN)


def test_user_sees_own_memberships_across_orgs_but_not_others(two_orgs):
    a, b = two_orgs
    user, _ = make_user("both@example.org")
    add_member(a.org, user)
    add_member(b.org, user)
    with transaction.atomic(), db_context(user_id=user.id):
        assert OrgMembership.all_objects.count() == 2  # own memberships only
        assert Organisation.objects.count() == 2
    outsider, _ = make_user("outsider@example.org")
    with transaction.atomic(), db_context(user_id=outsider.id):
        assert OrgMembership.all_objects.count() == 0
        assert Organisation.objects.count() == 0


def test_tenant_manager_refuses_without_context(two_orgs):
    with pytest.raises(NoTenantContext):
        list(OrgMembership.objects.all())


def test_audit_events_are_tenant_isolated(two_orgs):
    a, b = two_orgs
    with transaction.atomic():
        with db_context(org_id=a.org.id):
            record("test.a", organisation_id=a.org.id)
        with db_context(org_id=b.org.id):
            record("test.b", organisation_id=b.org.id)
            assert list(AuditEvent.objects.values_list("action", flat=True)) == ["test.b"]
        with db_context(org_id=a.org.id):
            with pytest.raises(DatabaseError, match="row-level security"), transaction.atomic():
                record("test.cross", organisation_id=b.org.id)


def test_platform_role_cannot_read_tenant_rows_without_context(two_orgs):
    with acting_as("app_platform"), transaction.atomic():
        assert _count("SELECT count(*) FROM org_membership") == 0
        assert _count("SELECT count(*) FROM organisation") == 2  # provisioning needs this
