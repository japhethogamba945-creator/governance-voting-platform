import pytest
from django.db import DatabaseError, connection, transaction

from audit.models import AuditEvent
from audit.services import ForbiddenAuditDetail, record
from platform_core.db import acting_as, db_context
from tenancy.models import Organisation, OrgMembership, RoleAssignment
from tenancy.roles import Role
from tenancy.services import provision_organisation

pytestmark = pytest.mark.django_db


def _event(two_orgs):
    a, _ = two_orgs
    with transaction.atomic(), db_context(org_id=a.org.id):
        return record("test.event", organisation_id=a.org.id, details={"k": "v"}), a


def test_app_rw_cannot_update_or_delete_audit_events(two_orgs):
    event, a = _event(two_orgs)
    with transaction.atomic(), db_context(org_id=a.org.id):
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditEvent.objects.filter(pk=event.pk).update(action="tampered")
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditEvent.objects.filter(pk=event.pk).delete()


def test_even_owner_cannot_rewrite_or_delete_audit_content(two_orgs):
    event, _ = _event(two_orgs)
    with acting_as(None):
        with pytest.raises(DatabaseError, match="immutable"), transaction.atomic():
            AuditEvent.objects.filter(pk=event.pk).update(action="tampered")
        with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
            AuditEvent.objects.filter(pk=event.pk).delete()


def test_even_owner_cannot_truncate_audit_events():
    # No fixture writes here: TRUNCATE refuses earlier if FK trigger events are pending.
    with acting_as(None):
        with pytest.raises(DatabaseError, match="cannot be truncated"), transaction.atomic(), \
                connection.cursor() as cursor:
            cursor.execute("TRUNCATE audit_event")


def test_chainer_may_fill_chain_fields_once(two_orgs):
    event, _ = _event(two_orgs)
    with acting_as("app_audit"):
        AuditEvent.objects.filter(pk=event.pk).update(seq=1, prev_hash=b"\0" * 32,
                                                      hash=b"\1" * 32)
        with pytest.raises(DatabaseError, match="already chained"), transaction.atomic():
            AuditEvent.objects.filter(pk=event.pk).update(hash=b"\2" * 32)
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditEvent.objects.filter(pk=event.pk).update(action="x")  # no column grant


def test_ballot_content_is_rejected_from_audit_details():
    with pytest.raises(ForbiddenAuditDetail):
        record("x", details={"nested": {"selections": ["c1"]}})


def test_provisioning_as_platform_role():
    with acting_as("app_platform"):
        org, user = provision_organisation(slug="cedar-club", legal_name="Cedar Club (fictional)",
                                           admin_email="Chair@Cedar.Example", using="default")
    assert user.email == "chair@cedar.example" and user.status == "invited"
    with acting_as(None):
        membership = OrgMembership.all_objects.get(organisation=org, user=user)
        assert list(RoleAssignment.all_objects.filter(membership=membership)
                    .values_list("role", flat=True)) == [Role.ORG_ADMIN]
        assert AuditEvent.objects.filter(action="organisation.provisioned",
                                         organisation=org).count() == 1


def test_app_rw_cannot_provision_organisations():
    with pytest.raises(DatabaseError), transaction.atomic():
        Organisation.objects.create(slug="rogue-club", legal_name="Rogue")
