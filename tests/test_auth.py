import time

import pytest
from django.db import connection
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts import throttle
from accounts.models import RecoveryCodeDevice
from accounts.views import GENERIC_CODE_ERROR, GENERIC_LOGIN_ERROR, LOCKED_ERROR
from audit.models import AuditEvent
from platform_core.db import acting_as
from tests.factories import PASSWORD, make_user, totp_now
from tests.helpers import login, without_csrf

pytestmark = pytest.mark.django_db


def test_full_login_reaches_home(client):
    user, device = make_user("member@example.org")
    login(client, user, device)
    assert client.get("/").status_code == 200


def test_password_alone_is_not_enough(client):
    user, _ = make_user("member@example.org")
    r = client.post("/accounts/login/", {"email": user.email, "password": PASSWORD})
    assert r.url == "/accounts/login/verify/"
    home = client.get("/")
    assert home.status_code == 302 and home.url.startswith("/accounts/login/")


def test_wrong_password_and_unknown_email_look_identical(client):
    make_user("member@example.org")
    wrong = client.post("/accounts/login/", {"email": "member@example.org", "password": "nope"})
    unknown = client.post("/accounts/login/", {"email": "nobody@example.org", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 400
    assert GENERIC_LOGIN_ERROR in wrong.content.decode()
    assert without_csrf(wrong.content) == without_csrf(
        unknown.content.replace(b"nobody@", b"member@"))


def test_invited_user_cannot_log_in(client):
    user, _ = make_user("invitee@example.org")
    with acting_as(None):
        type(user).objects.filter(pk=user.pk).update(status="invited")
    r = client.post("/accounts/login/", {"email": user.email, "password": PASSWORD})
    assert r.status_code == 400


def test_account_lockout_after_five_failures(client):
    user, device = make_user("member@example.org")
    for _ in range(5):
        client.post("/accounts/login/", {"email": user.email, "password": "wrong"})
    r = client.post("/accounts/login/", {"email": user.email, "password": PASSWORD})
    assert LOCKED_ERROR in r.content.decode()


def test_lockout_applies_equally_to_unknown_emails(client):
    for _ in range(5):
        client.post("/accounts/login/", {"email": "ghost@example.org", "password": "wrong"})
    r = client.post("/accounts/login/", {"email": "ghost@example.org", "password": "x"})
    assert LOCKED_ERROR in r.content.decode()


def test_throttle_keys_do_not_store_email():
    throttle.record_login_failure("member@example.org", "203.0.113.9")
    with connection.cursor() as cursor:
        cursor.execute("SELECT key FROM accounts_throttle")
        keys = [r[0] for r in cursor.fetchall()]
    assert all("example.org" not in k for k in keys)


def test_wrong_totp_rejected(client):
    user, device = make_user("member@example.org")
    client.post("/accounts/login/", {"email": user.email, "password": PASSWORD})
    r = client.post("/accounts/login/verify/", {"token": "000000"})
    assert r.status_code == 400 and GENERIC_CODE_ERROR in r.content.decode()


def test_session_key_rotates_on_login(client):
    user, device = make_user("member@example.org")
    client.get("/accounts/login/")
    client.session.save()
    before = client.session.session_key
    login(client, user, device)
    assert client.session.session_key != before


def test_first_login_enrols_totp_and_shows_recovery_codes_once(client):
    user, _ = make_user("new@example.org", with_totp=False)
    r = client.post("/accounts/login/", {"email": user.email, "password": PASSWORD})
    assert r.url == "/accounts/mfa/setup/"
    page = client.get("/accounts/mfa/setup/")
    assert page.status_code == 200 and b"data:image/svg+xml" in page.content
    reload = client.get("/accounts/mfa/setup/")  # reuses the unconfirmed device
    assert reload.status_code == 200 and page.content.count(b"secret") == \
        reload.content.count(b"secret")
    device = TOTPDevice.objects.get(user=user, confirmed=False)
    r = client.post("/accounts/mfa/setup/", {"token": totp_now(device)})
    assert r.url == "/accounts/mfa/recovery-codes/"
    codes_page = client.get("/accounts/mfa/recovery-codes/").content.decode()
    assert codes_page.count("<li><code>") == 10
    client.post("/accounts/mfa/recovery-codes/")
    assert client.get("/accounts/mfa/recovery-codes/").status_code == 302  # shown once
    # Platform-level events are write-only for app_rw; read them as admin.
    with acting_as(None):
        assert AuditEvent.objects.filter(action="auth.mfa_enrolled", actor=user).count() == 1


def test_recovery_code_is_single_use_and_stored_hashed(client):
    user, device = make_user("member@example.org")
    with acting_as(None):
        rdev = RecoveryCodeDevice.objects.create(user=user, name="r", confirmed=True)
        codes = rdev.generate_codes()
    with connection.cursor() as cursor:
        cursor.execute("SELECT code_hash FROM accounts_recovery_code")
        stored = [r[0] for r in cursor.fetchall()]
    assert not set(c.replace("-", "") for c in codes) & set(stored)

    client.post("/accounts/login/", {"email": user.email, "password": PASSWORD})
    r = client.post("/accounts/login/verify/", {"token": codes[0]})
    assert r.status_code == 302
    client.post("/accounts/logout/")
    client.post("/accounts/login/", {"email": user.email, "password": PASSWORD})
    r = client.post("/accounts/login/verify/", {"token": codes[0]})
    assert r.status_code == 400


def test_totp_code_cannot_be_replayed(client):
    user, device = make_user("member@example.org")
    login(client, user, device)
    client.post("/accounts/logout/")
    client.post("/accounts/login/", {"email": user.email, "password": PASSWORD})
    with acting_as(None):
        device.refresh_from_db()
    r = client.post("/accounts/login/verify/", {"token": totp_now(device)})
    assert r.status_code == 400


def test_idle_timeout_logs_out(client, settings):
    user, device = make_user("member@example.org")
    login(client, user, device)
    session = client.session
    session["last_seen"] = time.time() - settings.SESSION_IDLE_TIMEOUT - 1
    session.save()
    r = client.get("/")
    assert r.status_code == 302 and r.url.startswith("/accounts/login/")


def test_open_redirect_rejected(client):
    user, device = make_user("member@example.org")
    client.post("/accounts/login/?next=https://evil.example/", {"email": user.email,
                                                                "password": PASSWORD})
    r = client.post("/accounts/login/verify/", {"token": totp_now(device)})
    assert r.url == "/"


def test_logout_requires_post(client):
    user, device = make_user("member@example.org")
    login(client, user, device)
    assert client.get("/accounts/logout/").status_code == 405
