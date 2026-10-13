import time

import pytest

from accounts.stepup import step_up_required
from tenancy.roles import ROLE_PERMISSIONS, Perm, Role, permissions_for
from tests.factories import add_member, make_user, totp_now
from tests.helpers import login

pytestmark = pytest.mark.django_db


def test_every_role_has_a_permission_entry():
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_only_org_admin_manages_settings_or_roles():
    for role in Role:
        perms = permissions_for([role])
        assert (Perm.ORG_MANAGE_SETTINGS in perms) == (role == Role.ORG_ADMIN)
        assert (Perm.ROLES_GRANT in perms) == (role == Role.ORG_ADMIN)


def test_member_without_permission_gets_403(client, two_orgs):
    a, _ = two_orgs
    user, device = make_user("member@example.org")
    add_member(a.org, user, Role.MEMBER)
    login(client, user, device)
    assert client.get(f"/o/{a.org.slug}/").status_code == 200
    assert client.get(f"/o/{a.org.slug}/settings/").status_code == 403


def test_org_admin_can_open_settings(client, two_orgs):
    a, _ = two_orgs
    login(client, a.admin, a.admin_device)
    assert client.get(f"/o/{a.org.slug}/settings/").status_code == 200


def test_official_idle_timeout_is_stricter(client, two_orgs, settings):
    a, _ = two_orgs
    login(client, a.admin, a.admin_device)
    session = client.session
    session["last_seen"] = time.time() - settings.OFFICIAL_IDLE_TIMEOUT - 1
    session.save()
    r = client.get(f"/o/{a.org.slug}/")
    assert r.status_code == 302 and r.url.startswith("/accounts/login/")


def test_step_up_redirects_when_mfa_is_stale(rf, client, two_orgs):
    a, _ = two_orgs
    login(client, a.admin, a.admin_device)

    @step_up_required
    def privileged(request):
        from django.http import HttpResponse
        return HttpResponse("done")

    request = rf.get("/o/acacia-club/privileged/")
    request.session = {"mfa_at": time.time() - 3600}
    response = privileged(request)
    assert response.status_code == 302 and "/accounts/step-up/" in response.url
    request.session = {"mfa_at": time.time()}
    assert privileged(request).status_code == 200


def test_step_up_view_accepts_fresh_code(client, two_orgs):
    a, _ = two_orgs
    login(client, a.admin, a.admin_device)
    session = client.session
    session["mfa_at"] = 0
    session.save()
    a.admin_device.refresh_from_db()
    r = client.post("/accounts/step-up/?next=/o/acacia-club/",
                    {"token": totp_now(a.admin_device, offset_steps=1)})
    assert r.status_code == 302 and r.url == "/o/acacia-club/"
    assert client.session["mfa_at"] > time.time() - 5


def test_security_headers(client):
    r = client.get("/accounts/login/")
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "same-origin"


def test_authenticated_pages_are_not_cached(client, two_orgs):
    a, _ = two_orgs
    login(client, a.admin, a.admin_device)
    assert client.get("/").headers["Cache-Control"] == "no-store"


def test_healthz(client):
    assert client.get("/healthz").content == b"ok"
