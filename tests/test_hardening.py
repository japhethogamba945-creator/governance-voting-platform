"""Findings F-1 (ballot submission rate limit) and F-2 (breached passwords)."""

import hashlib
import re

import pytest
from django.contrib.auth.password_validation import validate_password
from django.core.checks import run_checks
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client

from accounts.validators import BreachedPasswordValidator
from platform_core.db import acting_as
from tests.helpers import login
from tests.test_voting import ballots, entitlement_status, ids, world  # noqa: F401

BREACHED = "Tr0ub4dor&3-but-much-longer"  # fictional, written into the test mirror


# --- F-1: ballot submissions per member per minute ------------------------------------

def _review(client, base, w, positions):
    return client.post(f"{base}review/", {"candidate": [str(i) for i in ids(w, *positions)]})


def _expire_window(user):
    with acting_as(None), connection.cursor() as cursor:
        cursor.execute("UPDATE accounts_throttle SET window_start = now() - interval '2 minutes', "
                       "locked_until = now() - interval '1 minute' WHERE key = %s",
                       [f"ballot:{user.id}"])


@pytest.mark.django_db
def test_ballot_submissions_are_rate_limited_per_member(client, world, settings):  # noqa: F811
    settings.BALLOT_POSTS_PER_MINUTE = 10
    w = world
    _, user, device = w["voters"][0]
    login(client, user, device)
    base = f"/o/{w['org'].slug}/elections/{w['election'].id}/ballot/"
    statuses = [_review(client, base, w, (0, 1, 2)).status_code for _ in range(10)]
    assert statuses == [400] * 10  # over-selection, but each one counts
    refused = _review(client, base, w, (0,))
    assert refused.status_code == 429 and refused.headers["Retry-After"] == "60"
    assert "no ballot was recorded" in refused.content.decode()
    # Another member is not affected.
    other = Client()
    login(other, w["voters"][1][1], w["voters"][1][2])
    assert _review(other, base, w, (0,)).status_code == 200
    # After the minute, voting works normally.
    _expire_window(user)
    review = _review(client, base, w, (1,)).content.decode()
    key = re.search(r'name="idempotency_key" value="([^"]+)"', review).group(1)
    cast = client.post(f"{base}cast/", {"candidate": [str(i) for i in ids(w, 1)],
                                        "idempotency_key": key, "receipt_commitment": ""})
    assert "Your ballot was accepted" in cast.content.decode()
    assert len(ballots(w["election"])) == 1


@pytest.mark.django_db
def test_a_throttled_retry_is_never_told_a_false_not_recorded(client, world, settings):  # noqa: F811
    """Acceptance test 15 still holds under the limit: a retry after a lost response that
    is throttled learns that the ballot was accepted; one that never voted is told nothing
    was recorded, and nothing was."""
    settings.BALLOT_POSTS_PER_MINUTE = 2
    w = world
    member, user, device = w["voters"][0]
    login(client, user, device)
    base = f"/o/{w['org'].slug}/elections/{w['election'].id}/ballot/"
    review = _review(client, base, w, (0,)).content.decode()
    key = re.search(r'name="idempotency_key" value="([^"]+)"', review).group(1)
    form = {"candidate": [str(i) for i in ids(w, 0)], "idempotency_key": key,
            "receipt_commitment": ""}
    assert client.post(f"{base}cast/", form).status_code == 200  # accepted (2 of 2)
    retry = client.post(f"{base}cast/", form)  # the response was "lost"; now throttled
    assert retry.status_code == 409 and "already been accepted" in retry.content.decode()
    assert len(ballots(w["election"])) == 1

    other = Client()
    _, user2, device2 = w["voters"][1]
    login(other, user2, device2)
    review = _review(other, base, w, (1,)).content.decode()
    key2 = re.search(r'name="idempotency_key" value="([^"]+)"', review).group(1)
    _review(other, base, w, (1,))  # second submission uses up the limit
    refused = other.post(f"{base}cast/", {"candidate": [str(i) for i in ids(w, 1)],
                                          "idempotency_key": key2, "receipt_commitment": ""})
    assert refused.status_code == 429 and "no ballot was recorded" in refused.content.decode()
    assert entitlement_status(w, w["voters"][1][0]) == "issued"
    assert len(ballots(w["election"])) == 1


# --- F-2: breached passwords ----------------------------------------------------------------

@pytest.fixture
def mirror(tmp_path, settings):
    digest = hashlib.sha1(BREACHED.encode(), usedforsecurity=False).hexdigest().upper()
    (tmp_path / f"{digest[:5]}.txt").write_text(
        f"{'0' * 35}:3\r\n{digest[5:]}:42\r\n{'F' * 35}:1\r\n", encoding="ascii")
    settings.BREACHED_PASSWORDS_DIR = str(tmp_path)
    return tmp_path


def test_breached_passwords_are_refused(mirror):
    with pytest.raises(ValidationError) as excinfo:
        validate_password(BREACHED)
    assert any(e.code == "password_breached" for e in excinfo.value.error_list)
    validate_password("an unlisted fictional passphrase 7Q4")  # other validators also pass
    assert BreachedPasswordValidator(directory=str(mirror)).is_breached(BREACHED)


def test_breached_check_tolerates_a_partial_mirror_and_is_off_without_one(mirror, settings):
    validator = BreachedPasswordValidator(directory=str(mirror))
    assert not validator.is_breached("a password whose prefix file is absent 1")
    settings.BREACHED_PASSWORDS_DIR = ""
    assert not BreachedPasswordValidator().is_breached(BREACHED)


@pytest.mark.django_db
def test_invitation_password_form_uses_the_check(mirror):
    from members.forms import AcceptInvitationForm

    form = AcceptInvitationForm({"password1": BREACHED, "password2": BREACHED}, user=None)
    assert not form.is_valid() and "data breach" in str(form.errors)


def test_deploy_check_warns_without_a_mirror(settings):
    settings.DEBUG = False
    settings.BREACHED_PASSWORDS_DIR = ""
    assert "accounts.W001" in {m.id for m in run_checks(include_deployment_checks=True)}
    settings.BREACHED_PASSWORDS_DIR = "/srv/pwned"
    assert "accounts.W001" not in {m.id for m in run_checks(include_deployment_checks=True)}
