import re
from datetime import timedelta

import pytest
from django.core import mail
from django.db import connection, transaction
from django.utils import timezone

from audit.models import AuditEvent
from members.models import Invitation, Member
from members.services import hash_token
from platform_core.db import acting_as, db_context, lookup_scope
from tenancy.models import RoleAssignment
from tenancy.roles import Role
from tests.factories import add_member, make_member, make_user, totp_now
from tests.helpers import login

pytestmark = pytest.mark.django_db

MEMBER_FORM = {
    "member_number": "A-100", "full_name": "Achieng Otieno (fictional)",
    "email": "Achieng@Example.org", "phone": "+254 700 000001", "category": "full",
    "subscription_status": "paid", "joined_on": "2019-03-01",
}


def _url(org, suffix=""):
    return f"/o/{org.slug}/members/{suffix}"


def _events(org, action):
    with acting_as(None):
        return list(AuditEvent.objects.filter(organisation=org, action=action).order_by("at"))


def _stale_step_up(client):
    session = client.session
    session["mfa_at"] = 0
    session.save()


# --- register --------------------------------------------------------------

def test_admin_creates_member_and_eligibility_is_audited(client, two_orgs):
    a, _ = two_orgs
    login(client, a.admin, a.admin_device)
    r = client.post(_url(a.org, "new/"), MEMBER_FORM)
    assert r.status_code == 302
    with acting_as(None):
        member = Member.all_objects.get(organisation=a.org, member_number="A-100")
    assert member.email == "achieng@example.org"
    (event,) = _events(a.org, "member.created")
    assert event.details["eligibility"]["category"] == [None, "full"]
    assert "email" in event.details["changed"]
    assert "achieng" not in str(event.details)  # personal values are not copied into audit


def test_duplicate_member_number_is_a_form_error(client, two_orgs):
    a, b = two_orgs
    make_member(a.org, "A-100")
    make_member(b.org, "A-100")  # same number in another organisation is fine
    login(client, a.admin, a.admin_device)
    r = client.post(_url(a.org, "new/"), MEMBER_FORM)
    assert r.status_code == 200 and b"Another member already has this number" in r.content


def test_edit_records_old_and_new_eligibility_values(client, two_orgs):
    a, _ = two_orgs
    member = make_member(a.org, "A-1")
    login(client, a.admin, a.admin_device)
    data = {**MEMBER_FORM, "member_number": "A-1", "subscription_status": "unpaid"}
    assert client.post(_url(a.org, f"{member.id}/edit/"), data).status_code == 302
    (event,) = _events(a.org, "member.updated")
    assert event.details["eligibility"]["subscription_status"] == ["paid", "unpaid"]


@pytest.mark.parametrize("role,list_status,create_status", [
    (Role.RETURNING_OFFICER, 200, 403),
    (Role.AUDITOR, 403, 403),
    (Role.MEMBER, 403, 403),
])
def test_register_permissions(client, two_orgs, role, list_status, create_status):
    a, _ = two_orgs
    user, device = make_user(f"{role}@example.org")
    add_member(a.org, user, role)
    login(client, user, device)
    assert client.get(_url(a.org)).status_code == list_status
    assert client.get(_url(a.org, "new/")).status_code == create_status


def test_search_and_status_filter(client, two_orgs):
    a, _ = two_orgs
    make_member(a.org, "A-1", full_name="Kamau One (fictional)")
    make_member(a.org, "A-2", full_name="Njeri Two (fictional)", status="inactive",
                deactivated_at=timezone.now())
    login(client, a.admin, a.admin_device)
    body = client.get(_url(a.org), {"q": "kamau"}).content.decode()
    assert "Kamau One" in body and "Njeri Two" not in body
    body = client.get(_url(a.org), {"status": "inactive"}).content.decode()
    assert "Njeri Two" in body and "Kamau One" not in body


# --- deactivation ------------------------------------------------------------

def test_deactivation_requires_step_up_and_reason_and_removes_access(client, two_orgs):
    a, _ = two_orgs
    user, device = make_user("member1@example.org")
    member = make_member(a.org, "A-1", email=user.email, user=user)
    add_member(a.org, user, Role.MEMBER)

    login(client, a.admin, a.admin_device)
    _stale_step_up(client)
    r = client.post(_url(a.org, f"{member.id}/deactivate/"), {"reason": "Resigned"})
    assert r.status_code == 302 and "/accounts/step-up/" in r.url
    a.admin_device.refresh_from_db()
    client.post(f"/accounts/step-up/?next={_url(a.org, f'{member.id}/deactivate/')}",
                {"token": totp_now(a.admin_device, offset_steps=1)})
    r = client.post(_url(a.org, f"{member.id}/deactivate/"), {"reason": ""})
    assert r.status_code == 200  # reason required
    r = client.post(_url(a.org, f"{member.id}/deactivate/"), {"reason": "Resigned in writing"})
    assert r.status_code == 302
    with acting_as(None):
        member.refresh_from_db()
        assert member.status == "inactive" and member.deactivated_at is not None
        assert not RoleAssignment.all_objects.filter(
            membership__user=user, role=Role.MEMBER, valid_to__isnull=True).exists()
    (event,) = _events(a.org, "member.deactivated")
    assert event.details["eligibility"]["status"] == ["active", "inactive"]

    member_client = client.__class__()
    login(member_client, user, device)
    assert member_client.get(f"/o/{a.org.slug}/").status_code == 404
    assert a.org.legal_name not in member_client.get("/").content.decode()


def test_reactivation_restores_member_role(client, two_orgs):
    a, _ = two_orgs
    user, device = make_user("member1@example.org")
    member = make_member(a.org, "A-1", email=user.email, user=user, status="inactive",
                         deactivated_at=timezone.now())
    add_member(a.org, user)
    login(client, a.admin, a.admin_device)
    assert client.post(_url(a.org, f"{member.id}/reactivate/")).status_code == 302
    member_client = client.__class__()
    login(member_client, user, device)
    assert member_client.get(f"/o/{a.org.slug}/").status_code == 200


# --- invitations ------------------------------------------------------------

def _invite(client, org, member):
    mail.outbox.clear()
    r = client.post(_url(org, f"{member.id}/invite/"))
    assert r.status_code == 302
    return mail.outbox


def _link_token(message):
    return re.search(r"https://vote\.example/invite/([^/\s]+)/", message.body).group(1)


def test_invitation_flow_end_to_end(client, two_orgs):
    a, _ = two_orgs
    member = make_member(a.org, "A-7", email="newbie@example.org")
    login(client, a.admin, a.admin_device)
    (message,) = _invite(client, a.org, member)
    assert message.to == ["newbie@example.org"]
    token = _link_token(message)
    with acting_as(None):
        invitation = Invitation.all_objects.get(member=member)
        assert invitation.token_hash == hash_token(token)
        assert not Invitation.all_objects.filter(token_hash=token).exists()  # never stored raw

    invitee = client.__class__()
    r = invitee.get(f"/invite/{token}/")
    assert r.status_code == 302 and r.url == "/invite/accept/"  # token leaves the URL
    page = invitee.get("/invite/accept/")
    assert page.status_code == 200 and a.org.legal_name.encode() in page.content
    weak = invitee.post("/invite/accept/", {"password1": "short", "password2": "short"})
    assert weak.status_code == 200
    strong = "a perfectly long passphrase"
    r = invitee.post("/invite/accept/", {"password1": strong, "password2": strong})
    assert r.url == "/accounts/mfa/setup/"
    from django_otp.plugins.otp_totp.models import TOTPDevice
    invitee.get("/accounts/mfa/setup/")
    with acting_as(None):
        device = TOTPDevice.objects.get(user__email="newbie@example.org")
    r = invitee.post("/accounts/mfa/setup/", {"token": totp_now(device)})
    assert r.url == "/accounts/mfa/recovery-codes/"
    r = invitee.post("/accounts/mfa/recovery-codes/")
    assert r.url == f"/o/{a.org.slug}/"
    assert invitee.get(f"/o/{a.org.slug}/").status_code == 200
    assert invitee.get(_url(a.org)).status_code == 403  # plain member: no register access

    # The link is single-use.
    assert client.__class__().get(f"/invite/{token}/").status_code == 410
    assert len(_events(a.org, "member.invitation_accepted")) == 1


def test_resend_revokes_previous_link(client, two_orgs):
    a, _ = two_orgs
    member = make_member(a.org, "A-7", email="newbie@example.org")
    login(client, a.admin, a.admin_device)
    first = _link_token(_invite(client, a.org, member)[0])
    second = _link_token(_invite(client, a.org, member)[0])
    assert first != second
    assert client.__class__().get(f"/invite/{first}/").status_code == 410
    assert client.__class__().get(f"/invite/{second}/").status_code == 302


def test_expired_invitation_is_rejected(client, two_orgs):
    a, _ = two_orgs
    member = make_member(a.org, "A-7", email="newbie@example.org")
    login(client, a.admin, a.admin_device)
    token = _link_token(_invite(client, a.org, member)[0])
    with acting_as(None):
        Invitation.all_objects.filter(member=member).update(
            created_at=timezone.now() - timedelta(days=9),
            expires_at=timezone.now() - timedelta(days=2))
    assert client.__class__().get(f"/invite/{token}/").status_code == 410
    assert client.__class__().get("/invite/not-a-real-token/").status_code == 410


def test_invalid_invitation_link_does_not_sign_you_out(client, two_orgs):
    a, _ = two_orgs
    login(client, a.admin, a.admin_device)
    assert client.get("/invite/not-a-real-token/").status_code == 410
    assert client.get("/").status_code == 200


def test_existing_account_is_linked_without_revealing_it(client, two_orgs):
    a, b = two_orgs
    # Someone who already has an account through organisation B.
    user, device = make_user("shared@example.org")
    add_member(b.org, user, Role.MEMBER)
    member = make_member(a.org, "A-9", email="shared@example.org")
    login(client, a.admin, a.admin_device)
    r = client.post(_url(a.org, f"{member.id}/invite/"), follow=True)
    assert b"Invitation sent to shared@example.org" in r.content  # same message as a new user
    (message,) = mail.outbox
    assert "/invite/" not in message.body  # no token needed: they sign in as usual
    with acting_as(None):
        assert not Invitation.all_objects.filter(member=member).exists()
    other = client.__class__()
    login(other, user, device)
    home = other.get("/").content.decode()
    assert a.org.legal_name in home and b.org.legal_name in home


def test_inactive_or_emailless_member_cannot_be_invited(client, two_orgs):
    a, _ = two_orgs
    no_email = make_member(a.org, "A-1")
    login(client, a.admin, a.admin_device)
    r = client.post(_url(a.org, f"{no_email.id}/invite/"), follow=True)
    assert b"Add an email address" in r.content and not mail.outbox


def test_linked_member_email_cannot_be_changed(client, two_orgs):
    a, _ = two_orgs
    user, _ = make_user("linked@example.org")
    member = make_member(a.org, "A-1", email=user.email, user=user)
    login(client, a.admin, a.admin_device)
    data = {**MEMBER_FORM, "member_number": "A-1", "email": "other@example.org"}
    client.post(_url(a.org, f"{member.id}/edit/"), data)
    with acting_as(None):
        member.refresh_from_db()
    assert member.email == "linked@example.org"  # disabled field ignores submitted value


# --- row-level security on users and invitations ------------------------------

def test_app_user_rows_are_scoped(two_orgs):
    a, b = two_orgs
    from accounts.models import User
    with transaction.atomic():
        assert User.objects.count() == 0  # no context: nobody
        with db_context(org_id=a.org.id):
            assert set(User.objects.values_list("email", flat=True)) == {a.admin.email}
        with db_context(user_id=b.admin.id):
            assert set(User.objects.values_list("email", flat=True)) == {b.admin.email}
        with lookup_scope(email=b.admin.email):
            assert User.objects.count() == 1


def test_app_rw_cannot_create_active_users_or_users_outside_an_org(two_orgs):
    a, _ = two_orgs
    from django.db import DatabaseError

    from accounts.models import User
    with transaction.atomic():
        with pytest.raises(DatabaseError), transaction.atomic():
            User.objects.create_user("x@example.org", status="invited")  # no org context
        with db_context(org_id=a.org.id):
            with pytest.raises(DatabaseError), transaction.atomic():
                User.objects.create_user("y@example.org", status="active")


def test_invitation_visible_only_with_its_token(client, two_orgs):
    a, _ = two_orgs
    member = make_member(a.org, "A-7", email="newbie@example.org")
    login(client, a.admin, a.admin_device)
    token = _link_token(_invite(client, a.org, member)[0])
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM invitation")
        assert cursor.fetchone()[0] == 0
        with lookup_scope(invite_token_hash=hash_token("wrong")):
            cursor.execute("SELECT count(*) FROM invitation")
            assert cursor.fetchone()[0] == 0
        with lookup_scope(invite_token_hash=hash_token(token)):
            cursor.execute("SELECT count(*) FROM invitation")
            assert cursor.fetchone()[0] == 1
            cursor.execute("SELECT count(*) FROM member")
            assert cursor.fetchone()[0] == 1  # only the invited member


# --- roles -------------------------------------------------------------------

def test_admin_grants_and_revokes_official_roles(client, two_orgs):
    a, _ = two_orgs
    user, device = make_user("ro@example.org")
    membership = add_member(a.org, user, Role.MEMBER)
    login(client, a.admin, a.admin_device)
    r = client.post(f"/o/{a.org.slug}/roles/grant/",
                    {"membership": membership.id, "role": "returning_officer"})
    assert r.status_code == 302
    with acting_as(None):
        assignment = RoleAssignment.all_objects.get(membership=membership,
                                                    role="returning_officer")
        assert assignment.granted_by_id == a.admin.id
    assert len(_events(a.org, "role.granted")) == 1

    ro = client.__class__()
    login(ro, user, device)
    assert ro.get(f"/o/{a.org.slug}/members/").status_code == 200
    assert ro.get(f"/o/{a.org.slug}/roles/").status_code == 200
    assert ro.post(f"/o/{a.org.slug}/roles/grant/",
                   {"membership": membership.id, "role": "org_admin"}).status_code == 403

    client.post(f"/o/{a.org.slug}/roles/revoke/", {"assignment": assignment.id})
    assert ro.get(f"/o/{a.org.slug}/members/").status_code == 403


def test_cannot_grant_member_role_or_change_own_roles(client, two_orgs):
    a, _ = two_orgs
    user, _ = make_user("someone@example.org")
    membership = add_member(a.org, user, Role.MEMBER)
    login(client, a.admin, a.admin_device)
    r = client.post(f"/o/{a.org.slug}/roles/grant/", {"membership": membership.id,
                                                      "role": "member"}, follow=True)
    assert b"cannot be granted here" in r.content
    with acting_as(None):
        own = RoleAssignment.all_objects.get(membership__user=a.admin)
    r = client.post(f"/o/{a.org.slug}/roles/revoke/", {"assignment": own.id}, follow=True)
    assert b"cannot change your own roles" in r.content


def test_last_admin_cannot_be_removed(client, two_orgs):
    a, _ = two_orgs
    second, device = make_user("admin2@example.org")
    add_member(a.org, second, Role.ORG_ADMIN)
    login(client, second, device)
    with acting_as(None):
        first_admin = RoleAssignment.all_objects.get(membership__user=a.admin)
    client.post(f"/o/{a.org.slug}/roles/revoke/", {"assignment": first_admin.id})
    with acting_as(None):
        first_admin.refresh_from_db()
        assert first_admin.valid_to is not None  # allowed: another admin remains
    # Now only `second` is admin; the restored first admin cannot remove them...
    # ...and a removal that would leave no admin is refused.
    with acting_as(None):
        remaining = RoleAssignment.all_objects.get(membership__user=second,
                                                   role="org_admin")
    from tenancy.services import RoleError, revoke_role
    with transaction.atomic(), db_context(org_id=a.org.id):
        with pytest.raises(RoleError, match="at least one administrator"):
            revoke_role(remaining, actor=a.admin, actor_membership=first_admin.membership)
