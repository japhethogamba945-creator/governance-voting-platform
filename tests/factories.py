"""Fictional test data. Fixtures are created with the admin login (RLS bypassed)."""

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import connection
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from platform_core.db import acting_as
from tenancy.models import Organisation, OrgMembership, RoleAssignment
from tenancy.roles import Role

PASSWORD = "correct horse battery staple"


@dataclass
class OrgFixture:
    org: Organisation
    admin: object
    admin_device: TOTPDevice


def make_user(email: str, *, password: str = PASSWORD, with_totp: bool = True):
    with acting_as(None):
        user = get_user_model().objects.create_user(email, password=password, status="active")
        device = None
        if with_totp:
            device = TOTPDevice.objects.create(user=user, name="test", confirmed=True)
    return user, device


def add_member(org: Organisation, user, *roles: Role) -> OrgMembership:
    with acting_as(None):
        membership = OrgMembership.all_objects.create(organisation=org, user=user)
        for role in roles:
            RoleAssignment.all_objects.create(organisation=org, membership=membership, role=role)
    return membership


def make_org_with_admin(slug: str, name: str) -> OrgFixture:
    with acting_as(None):
        org = Organisation.objects.create(slug=slug, legal_name=name)
    admin, device = make_user(f"admin@{slug}.example")
    add_member(org, admin, Role.ORG_ADMIN)
    return OrgFixture(org=org, admin=admin, admin_device=device)


def totp_now(device: TOTPDevice, offset_steps: int = 0) -> str:
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = totp.time + offset_steps * device.step
    return f"{totp.token():0{device.digits}d}"


def current_role() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        return cursor.fetchone()[0]


def make_member(org: Organisation, number: str = "M001", *, email: str = "",
                full_name: str = "Wanjiru Test (fictional)", **extra):
    from datetime import date

    from members.models import Member

    fields = {"category": "full", "subscription_status": "paid", "joined_on": date(2020, 1, 15),
              **extra}
    with acting_as(None):
        return Member.all_objects.create(organisation=org, member_number=number,
                                         full_name=full_name, email=email, **fields)


def make_import_batch(org: Organisation, created_by):
    from members.models import ImportBatch

    with acting_as(None):
        return ImportBatch.all_objects.create(organisation=org, created_by=created_by,
                                              file_sha256="0" * 64, rows=[], plan_digest="x")


def make_meeting(org: Organisation, created_by, title: str = "AGM 2026 (fictional)"):
    from django.utils import timezone

    from elections.models import Meeting

    with acting_as(None):
        return Meeting.all_objects.create(organisation=org, title=title, created_by=created_by,
                                          scheduled_at=timezone.now())


def make_election(org: Organisation, created_by, *, seats: int = 3, title: str = "Board 2026",
                  opens_in_days: int = 30, **extra):
    from datetime import timedelta

    from django.utils import timezone

    from elections.models import Election

    opens = timezone.now() + timedelta(days=opens_in_days)
    with acting_as(None):
        return Election.all_objects.create(
            organisation=org, title=title, seats=seats, created_by=created_by,
            opens_at=opens, closes_at=opens + timedelta(days=2), **extra)


def valid_rules(seats: int = 3, **nomination):
    from elections import rules

    r = rules.default_rules(seats, ("full", "life"))
    r["tie_procedure"]["description"] = "Lots are drawn by the Returning Officer in public."
    r["nomination"].update(nomination)
    return r


def open_nominations(election, created_by, rules: dict | None = None):
    """Give a draft election rules and move it to 'nomination' (admin login, no app checks)."""
    from elections.models import Election, ElectionRuleVersion

    with acting_as(None):
        version = ElectionRuleVersion.all_objects.create(
            organisation_id=election.organisation_id, election=election, version=1,
            rules=rules or valid_rules(election.seats), created_by=created_by)
        Election.all_objects.filter(pk=election.pk).update(current_rule_version=version)
        Election.all_objects.filter(pk=election.pk).update(state="nomination")
        return Election.all_objects.select_related("current_rule_version").get(pk=election.pk)


def make_nomination(org: Organisation, created_by, *, election=None, nominee=None):
    import uuid as _uuid

    from nominations.models import Nomination

    election = election or open_nominations(make_election(org, created_by), created_by)
    nominee = nominee or make_member(org, f"N-{_uuid.uuid4().hex[:8]}")
    with acting_as(None):
        from django.utils import timezone
        return Nomination.all_objects.create(
            organisation=org, election=election, nominee=nominee, submitted_by=nominee,
            nominee_consented_at=timezone.now())


def make_document(org: Organisation, created_by, *, nomination=None):
    import hashlib
    import uuid as _uuid

    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    from nominations.models import NominationDocument

    nomination = nomination or make_nomination(org, created_by)
    data = b"%PDF-1.4 fictional test document"
    name = default_storage.save(f"nominations/test/{_uuid.uuid4().hex}.pdf", ContentFile(data))
    with acting_as(None):
        return NominationDocument.all_objects.create(
            organisation=org, nomination=nomination, storage_name=name,
            original_name="statement.pdf", sha256=hashlib.sha256(data).hexdigest(),
            mime="application/pdf", size_bytes=len(data), uploaded_by=created_by)


KEYHOLDER_PASSPHRASE = "correct horse battery staple keyholder"


def make_keyholder(org: Organisation, user=None, *, role: Role = Role.AUDITOR,
                   passphrase: str = KEYHOLDER_PASSPHRASE):
    """An official with a keyholder key (created with the admin login)."""
    import uuid as _uuid

    from keys import crypto
    from keys.models import KeyholderKey

    if user is None:
        user, _ = make_user(f"keyholder-{_uuid.uuid4().hex[:6]}@example.org")
        add_member(org, user, role)
    material = crypto.new_keyholder_keypair(passphrase)
    with acting_as(None):
        KeyholderKey.all_objects.create(organisation=org, user=user,
                                        public_key=material["public_key"], salt=material["salt"],
                                        encrypted_private_key=material["encrypted_private_key"],
                                        kdf=material["kdf"])
    return user


def backdate_opening(election, minutes: int = 1) -> None:
    """TEST ONLY: move a scheduled election's opening time into the past.

    Scheduling requires a future opening time and the test runs inside one
    transaction, so the opening time is moved back here, bypassing the triggers
    with the superuser-only replication role.
    """
    with acting_as(None), connection.cursor() as cursor:
        cursor.execute("SET LOCAL session_replication_role = replica")
        cursor.execute("UPDATE election SET opens_at = now() - make_interval(mins => %s) "
                       "WHERE id = %s", [minutes, election.pk])
        cursor.execute("SET LOCAL session_replication_role = origin")
