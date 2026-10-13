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
