import itertools
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from nacl.public import PrivateKey, SealedBox

from audit.models import AuditEvent
from elections import lifecycle
from elections import services as election_services
from elections.models import Election
from electorate.models import ElectorateEntry, Entitlement
from electorate.services import electorate_digest, open_due_elections
from keys import crypto
from keys import services as key_services
from keys.models import KeyholderKey, KeyShare
from nominations import services as nomination_services
from nominations.models import Nomination
from platform_core.db import acting_as, db_context, scheduler_scope
from tenancy.models import RoleAssignment
from tenancy.roles import Role
from tests.factories import (
    KEYHOLDER_PASSPHRASE,
    add_member,
    backdate_opening,
    make_election,
    make_keyholder,
    make_member,
    make_user,
    open_nominations,
)
from tests.helpers import login

pytestmark = pytest.mark.django_db
RO = {Role.RETURNING_OFFICER}


# --- cryptography ----------------------------------------------------------------------

def test_any_two_of_four_bundles_rebuild_the_key_and_one_reveals_nothing():
    secret = PrivateKey.generate()
    bundles = crypto.split(bytes(secret), 4)
    for a, b in itertools.combinations(bundles, 2):
        key = crypto.rebuild_private_key(a, b, bytes(secret.public_key))
        assert bytes(key) == bytes(secret)
    # No single piece equals the secret, and every holder's pieces are distinct.
    for bundle in bundles:
        pieces = [crypto.base64.b64decode(p) for _, _, p in bundle]
        assert len(pieces) == 3 and bytes(secret) not in pieces


def test_mismatched_or_wrong_shares_are_rejected():
    first, second = crypto.split(bytes(PrivateKey.generate()), 4), crypto.split(
        bytes(PrivateKey.generate()), 4)
    with pytest.raises(crypto.KeyError_):
        crypto.rebuild_private_key(first[0], second[1], b"\0" * 32)


def test_keyholder_passphrase_rules_and_unlock():
    with pytest.raises(crypto.KeyError_, match="at least 20"):
        crypto.new_keyholder_keypair("too short")
    material = crypto.new_keyholder_keypair(KEYHOLDER_PASSPHRASE)
    private = crypto.unlock_keyholder(material["salt"], material["encrypted_private_key"],
                                      KEYHOLDER_PASSPHRASE)
    assert bytes(private.public_key) == material["public_key"]
    with pytest.raises(crypto.KeyError_, match="not correct"):
        crypto.unlock_keyholder(material["salt"], material["encrypted_private_key"],
                                KEYHOLDER_PASSPHRASE + "x")


def test_backup_code_round_trip():
    raw = bytes(range(32))
    code = crypto.format_backup_secret(raw)
    assert crypto.parse_backup_secret(code.lower().replace("-", " ")) == raw
    with pytest.raises(crypto.KeyError_):
        crypto.parse_backup_secret("not-a-code")


# --- the ceremony through the election lifecycle ------------------------------------------

@pytest.fixture
def org_a(two_orgs):
    """Org A with an RO keyholder, two other keyholders and an election awaiting approval."""
    a, _ = two_orgs
    ro, ro_device = make_user("ro@example.org")
    add_member(a.org, ro, Role.RETURNING_OFFICER)
    make_keyholder(a.org, ro)
    others = [make_keyholder(a.org), make_keyholder(a.org)]
    election = open_nominations(make_election(a.org, a.admin), a.admin)
    nominee = make_member(a.org, "CAND-1", full_name="Zawadi Candidate (fictional)")
    with transaction.atomic(), db_context(org_id=a.org.id):
        nomination_services.submit(election, submitter=nominee, nominee=nominee, proposer=None,
                                   seconder=None, statement="", actor=a.admin)
        election_services.transition(election, lifecycle.REVIEW, actor=ro, roles=RO)
        nomination_services.decide(Nomination.objects.get(election=election), approve=True,
                                   reason="", actor=ro)
        election_services.transition(election, lifecycle.READY, actor=ro, roles=RO)
    with acting_as(None):
        election = Election.all_objects.select_related("current_rule_version").get(pk=election.pk)
    return a, ro, ro_device, others, election


def _schedule(a, ro, election, keyholders):
    context = {"keyholder_user_ids": [str(u.id) for u in keyholders]}
    with transaction.atomic(), db_context(org_id=a.org.id, user_id=ro.id):
        election_services.transition(election, lifecycle.SCHEDULED, actor=ro, roles=RO,
                                     context=context)
    return context


def test_ceremony_creates_shares_and_any_two_rebuild_the_key(org_a):
    a, ro, _, others, election = org_a
    context = _schedule(a, ro, election, others)
    with acting_as(None):
        election = Election.all_objects.get(pk=election.pk)
        shares = list(KeyShare.all_objects.filter(election=election))
    assert election.state == "scheduled" and len(bytes(election.public_key)) == 32
    assert election.public_key_fpr == crypto.fingerprint(bytes(election.public_key))
    assert [s.share_index for s in shares] == [1, 2, 3, 4] and shares[3].keyholder_key_id is None
    backup_code = context["backup_secret"]

    holders = [{"user": ro, "passphrase": KEYHOLDER_PASSPHRASE},
               {"user": others[0], "passphrase": KEYHOLDER_PASSPHRASE},
               {"user": others[1], "passphrase": KEYHOLDER_PASSPHRASE},
               {"backup_code": backup_code}]
    with transaction.atomic(), db_context(org_id=a.org.id):
        for pair in itertools.combinations(holders, 2):
            key = key_services.rebuild_election_key(election, list(pair))
            # The rebuilt key opens a ballot sealed to the election's public key.
            sealed = SealedBox(key.public_key).encrypt(b"test ballot")
            assert SealedBox(key).decrypt(sealed) == b"test ballot"
        with pytest.raises(key_services.KeysError, match="not correct"):
            key_services.rebuild_election_key(election, [
                {"user": ro, "passphrase": "wrong passphrase, long enough"}, holders[1]])
        with pytest.raises(key_services.KeysError, match="Exactly two"):
            key_services.rebuild_election_key(election, holders[:1])
        with pytest.raises(key_services.KeysError, match="not a keyholder"):
            key_services.rebuild_election_key(election, [
                {"user": a.admin, "passphrase": KEYHOLDER_PASSPHRASE}, holders[1]])
    with acting_as(None):
        event = AuditEvent.objects.get(action="election.key_ceremony", object_id=election.id)
    assert backup_code.replace("-", "") not in str(event.details)  # never recorded


@pytest.mark.parametrize("case", ["one_only", "duplicate", "no_key", "not_official"])
def test_ceremony_validation(org_a, case):
    a, ro, _, others, election = org_a
    if case == "one_only":
        chosen = others[:1]
    elif case == "duplicate":
        chosen = [others[0], others[0]]
    elif case == "no_key":
        user, _ = make_user("nokey@example.org")
        add_member(a.org, user, Role.AUDITOR)
        chosen = [others[0], user]
    else:
        with acting_as(None):
            RoleAssignment.all_objects.filter(membership__user=others[1]).update(
                valid_to=timezone.now())
        chosen = others
    with pytest.raises(election_services.ElectionError):
        _schedule(a, ro, election, chosen)
    with acting_as(None):
        assert Election.all_objects.get(pk=election.pk).state == "ready_for_approval"


def test_database_requires_the_ceremony_to_schedule(org_a):
    a, ro, _, others, election = org_a
    with transaction.atomic(), db_context(org_id=a.org.id):
        # Approve the rules directly, then try to schedule without a key ceremony.
        version = election.current_rule_version
        version.approved_by, version.approved_at = ro, timezone.now()
        version.save(update_fields=["approved_by", "approved_at"])
        with pytest.raises(DatabaseError, match="key ceremony"), transaction.atomic():
            Election.objects.filter(pk=election.pk).update(state="scheduled")


def test_shares_cannot_be_changed_or_added_later(org_a):
    a, ro, _, others, election = org_a
    _schedule(a, ro, election, others)
    with transaction.atomic(), db_context(org_id=a.org.id):
        with pytest.raises(DatabaseError), transaction.atomic():
            KeyShare.objects.filter(election=election).update(holder_label="x")
        with pytest.raises(DatabaseError, match="only be created by the key ceremony"), \
                transaction.atomic():
            KeyShare.objects.create(organisation=a.org, election=election, share_index=9,
                                    holder_label="extra", encrypted_bundle=b"x",
                                    bundle_sha256="0" * 64)
    with acting_as(None), pytest.raises(DatabaseError, match="immutable"), \
            transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM key_share WHERE election_id = %s", [election.pk])


# --- opening: electorate and entitlements ---------------------------------------------------

def test_scheduler_opens_due_elections_with_a_frozen_electorate(org_a, two_orgs):
    a, ro, _, others, election = org_a
    # Members: CAND-1 (eligible, from the fixture) plus a mix.
    make_member(a.org, "OK-2", category="life", subscription_status="exempt")
    make_member(a.org, "NO-ASSOC", category="associate")
    make_member(a.org, "NO-UNPAID", subscription_status="unpaid")
    make_member(a.org, "NO-INACTIVE", status="inactive", deactivated_at=timezone.now())
    _schedule(a, ro, election, others)
    future = make_election(a.org, a.admin, title="Later")  # draft: must be ignored

    assert open_due_elections() == []  # not due yet
    backdate_opening(election)
    results = open_due_elections()
    assert results == [(str(election.id), "opened (2 voters)")]
    with acting_as(None):
        election.refresh_from_db()
        entries = list(ElectorateEntry.all_objects.filter(election=election))
        entitlements = Entitlement.all_objects.filter(election=election)
        assert election.state == "open"
        assert sorted(e.member.member_number for e in entries) == ["CAND-1", "OK-2"]
        assert entitlements.count() == 2 and set(entitlements.values_list("status", flat=True)) \
            == {"issued"}
        assert election.electorate_digest == electorate_digest(entries)
        assert Election.all_objects.get(pk=future.pk).state == "draft"
        assert AuditEvent.objects.filter(action="election.electorate_frozen",
                                         object_id=election.id).exists()
    assert open_due_elections() == []  # idempotent


def test_empty_electorate_keeps_the_election_scheduled_and_is_audited(org_a):
    a, ro, _, others, election = org_a
    _schedule(a, ro, election, others)
    with acting_as(None):
        from members.models import Member
        Member.all_objects.filter(organisation=a.org).update(subscription_status="unpaid")
    backdate_opening(election)
    (result,) = open_due_elections()
    assert result[1].startswith("failed: No members are eligible")
    with acting_as(None):
        assert Election.all_objects.get(pk=election.pk).state == "scheduled"
        assert AuditEvent.objects.filter(action="election.open_failed").count() == 1


def test_electorate_and_entitlement_guards(org_a):
    a, ro, _, others, election = org_a
    _schedule(a, ro, election, others)
    backdate_opening(election)
    open_due_elections()
    late = make_member(a.org, "LATE-1")
    with transaction.atomic(), db_context(org_id=a.org.id):
        with pytest.raises(DatabaseError, match="only be set when voting opens"), \
                transaction.atomic():
            ElectorateEntry.objects.create(organisation=a.org, election=election, member=late,
                                           member_category="full", subscription_status="paid")
        with pytest.raises(DatabaseError, match="permission denied"), transaction.atomic():
            ElectorateEntry.objects.filter(election=election).update(member_category="life")
        entitlement = Entitlement.objects.filter(election=election).first()
        with pytest.raises(DatabaseError, match="only be revoked while voting is suspended"), \
                transaction.atomic():
            Entitlement.objects.filter(pk=entitlement.pk).update(
                status="revoked", revoked_reason="x", revoked_by=ro)
        # issued -> consumed is the one change allowed while open (casting, 6.6) ...
        Entitlement.objects.filter(pk=entitlement.pk).update(
            status="consumed", consumed_on=date.today(), idempotency_hash=b"h" * 32)
        # ... and only once.
        with pytest.raises(DatabaseError, match="only change once"), transaction.atomic():
            Entitlement.objects.filter(pk=entitlement.pk).update(status="issued")
        with pytest.raises(DatabaseError, match="permission denied"), transaction.atomic(), \
                connection.cursor() as cursor:
            cursor.execute("DELETE FROM entitlement WHERE id = %s", [entitlement.pk])
    with acting_as(None):  # the owner bypasses grants but not the triggers
        for sql, message in (
                ("UPDATE electorate_entry SET member_category = 'life' WHERE election_id = %s",
                 "frozen"),
                ("DELETE FROM electorate_entry WHERE election_id = %s", "frozen"),
                ("DELETE FROM entitlement WHERE election_id = %s", "never deleted")):
            with pytest.raises(DatabaseError, match=message), transaction.atomic(), \
                    connection.cursor() as cursor:
                cursor.execute(sql, [election.pk])


def test_scheduler_scope_only_reveals_due_states(org_a, two_orgs):
    a, ro, _, others, election = org_a
    _, b = two_orgs
    make_election(b.org, b.admin, title="B draft")
    _schedule(a, ro, election, others)
    with transaction.atomic():
        assert Election.all_objects.count() == 0  # no context: nothing
        with scheduler_scope():
            visible = list(Election.all_objects.values_list("state", flat=True))
    assert visible == ["scheduled"]  # A's scheduled election only; B's draft stays hidden


def test_run_scheduler_command(org_a):
    a, ro, _, others, election = org_a
    _schedule(a, ro, election, others)
    backdate_opening(election)
    out = StringIO()
    call_command("run_scheduler", stdout=out)
    assert f"election {election.id}: opened (1 voters)" in out.getvalue()


# --- keyholder passphrase page --------------------------------------------------------------

def test_keyholder_passphrase_page(client, two_orgs):
    a, _ = two_orgs
    auditor, device = make_user("aud@example.org")
    add_member(a.org, auditor, Role.AUDITOR)
    login(client, auditor, device)
    url = f"/o/{a.org.slug}/keyholder/"
    r = client.post(url, {"passphrase": "short", "passphrase_again": "short"})
    assert r.status_code == 200 and b"at least 20" in r.content
    r = client.post(url, {"passphrase": KEYHOLDER_PASSPHRASE,
                          "passphrase_again": KEYHOLDER_PASSPHRASE})
    assert r.status_code == 302
    with acting_as(None):
        key = KeyholderKey.all_objects.get(user=auditor)
        assert KEYHOLDER_PASSPHRASE.encode() not in bytes(key.encrypted_private_key)
    # Changing it needs the current passphrase and keeps the same key pair.
    new = "another long keyholder passphrase"
    r = client.post(url, {"current_passphrase": "wrong but long enough phrase",
                          "passphrase": new, "passphrase_again": new})
    assert b"not correct" in r.content
    client.post(url, {"current_passphrase": KEYHOLDER_PASSPHRASE, "passphrase": new,
                      "passphrase_again": new})
    with acting_as(None):
        changed = KeyholderKey.all_objects.get(user=auditor)
    assert bytes(changed.public_key) == bytes(key.public_key)
    crypto.unlock_keyholder(bytes(changed.salt), bytes(changed.encrypted_private_key), new)


def test_members_cannot_become_keyholders(client, two_orgs):
    a, _ = two_orgs
    user, device = make_user("member@example.org")
    add_member(a.org, user, Role.MEMBER)
    login(client, user, device)
    assert client.get(f"/o/{a.org.slug}/keyholder/").status_code == 403
