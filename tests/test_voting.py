"""Secret ballot casting: acceptance tests 3, 4, 5, 6, 7, 8 and 15."""

import copy
import hashlib
import logging
import re
import threading
import uuid
from unittest import mock

import pytest
from django.core import mail
from django.db import DatabaseError, connection, connections, transaction

from audit.models import AuditEvent
from elections import lifecycle
from elections import services as election_services
from elections.models import Election
from electorate.models import Entitlement
from electorate.services import close_due_elections, open_due_elections
from keys import services as key_services
from nominations import services as nomination_services
from nominations.models import Candidate, Nomination
from platform_core.db import acting_as, db_context
from tenancy.models import Organisation
from tenancy.roles import Role
from tests.factories import (
    KEYHOLDER_PASSPHRASE,
    add_member,
    backdate_closing,
    backdate_opening,
    make_election,
    make_keyholder,
    make_member,
    make_user,
    open_nominations,
    valid_rules,
)
from tests.helpers import login
from voting import encoding, services
from voting.models import Ballot
from voting.services import BallotRejected, as_db_role

RO = {Role.RETURNING_OFFICER}
CANDIDATE_NAMES = ("Amina Canary-7Q4 (fictional)", "Baraka Otieno (fictional)",
                   "Chebet Wanjiku (fictional)")


def build_open_election(org, admin, *, seats=2, rules_overrides=None, voters=3):
    """An election in `open` state with three candidates, ``voters`` eligible voters
    (each with an account), one ineligible member with an account, and the
    backup code. Uses the real services throughout."""
    ro, ro_device = make_user(f"ro-{uuid.uuid4().hex[:6]}@example.org")
    add_member(org, ro, Role.RETURNING_OFFICER)
    make_keyholder(org, ro)
    others = [make_keyholder(org), make_keyholder(org)]
    rules = valid_rules(seats)
    rules.update(rules_overrides or {})
    election = open_nominations(make_election(org, admin, seats=seats), admin, rules=rules)
    candidates = [make_member(org, f"C{i}-{uuid.uuid4().hex[:4]}", full_name=name)
                  for i, name in enumerate(CANDIDATE_NAMES)]
    people = []
    for i in range(voters):
        user, device = make_user(f"voter{i}-{uuid.uuid4().hex[:6]}@example.org")
        add_member(org, user, Role.MEMBER)
        people.append((make_member(org, f"V{i}-{uuid.uuid4().hex[:4]}", email=user.email,
                                   user=user), user, device))
    outsider_user, outsider_device = make_user(f"assoc-{uuid.uuid4().hex[:6]}@example.org")
    add_member(org, outsider_user, Role.MEMBER)
    outsider = make_member(org, f"A-{uuid.uuid4().hex[:4]}", category="associate",
                           email=outsider_user.email, user=outsider_user)
    context = {"keyholder_user_ids": [str(o.id) for o in others]}
    with transaction.atomic(), db_context(org_id=org.id, user_id=ro.id):
        for c in candidates:
            nomination_services.submit(election, submitter=c, nominee=c, proposer=None,
                                       seconder=None, statement=f"Statement of {c.full_name}",
                                       actor=admin)
        election_services.transition(election, lifecycle.REVIEW, actor=ro, roles=RO)
        for n in Nomination.objects.filter(election=election):
            nomination_services.decide(n, approve=True, reason="", actor=ro)
        election_services.transition(election, lifecycle.READY, actor=ro, roles=RO)
        election_services.transition(election, lifecycle.SCHEDULED, actor=ro, roles=RO,
                                     context=context)
    backdate_opening(election)
    assert open_due_elections()[0][1].startswith("opened")
    with acting_as(None):
        election = Election.all_objects.select_related("current_rule_version").get(pk=election.pk)
        cands = list(Candidate.all_objects.filter(election=election).order_by("ballot_order"))
    return {"org": org, "election": election, "candidates": cands, "voters": people,
            "outsider": (outsider, outsider_user, outsider_device), "ro": (ro, ro_device),
            "backup_code": context["backup_secret"]}


@pytest.fixture
def world(db, two_orgs):
    a, _ = two_orgs
    return build_open_election(a.org, a.admin)


def cast_as(w, voter, selections, *, key=None, commitment=None):
    member, user, _ = voter
    with db_context(org_id=w["org"].id, user_id=user.id):
        if key is None:
            entitlement = Entitlement.objects.get(election=w["election"], member=member)
            key = services.issue_idempotency_key(entitlement.id)
        return services.cast(election_id=w["election"].id, member_id=member.id,
                             selections=[str(s) for s in selections], idempotency_key=key,
                             receipt_commitment=commitment, actor=user), key


def ids(w, *positions):
    return [w["candidates"][p].id for p in positions]


def ballots(election):
    with acting_as(None):
        return list(Ballot.objects.filter(election=election))


def entitlement_status(w, member):
    with acting_as(None):
        return Entitlement.all_objects.get(election=w["election"], member=member).status


# --- secrecy and correctness of an accepted ballot -------------------------------------

@pytest.mark.django_db
def test_accepted_ballot_is_sealed_and_has_no_voter_link(world):
    w = world
    with transaction.atomic():
        result, _ = cast_as(w, w["voters"][0], ids(w, 0, 2))
    assert result.status == "accepted" and len(result.receipt_code) == 16
    (ballot,) = ballots(w["election"])
    assert {f.name for f in Ballot._meta.get_fields()} == {
        "id", "organisation", "election", "ciphertext", "receipt_commitment"}
    assert len(bytes(ballot.ciphertext)) == encoding.CIPHERTEXT_BYTES
    assert bytes(ballot.receipt_commitment) == encoding.receipt_commitment(
        w["election"].id, result.receipt_code)
    # Only two keyholders together can read it.
    with transaction.atomic(), db_context(org_id=w["org"].id):
        key = key_services.rebuild_election_key(w["election"], [
            {"user": w["ro"][0], "passphrase": KEYHOLDER_PASSPHRASE},
            {"backup_code": w["backup_code"]}])
    content = encoding.decode(encoding.unseal(key, bytes(ballot.ciphertext)))
    assert content == {"e": str(w["election"].id),
                       "s": sorted(str(i) for i in ids(w, 0, 2)), "v": 1}
    assert entitlement_status(w, w["voters"][0][0]) == "consumed"
    with acting_as(None):
        event = AuditEvent.objects.get(action="ballot.accepted")
    assert event.details == {} and event.at.second == 0 and event.at.microsecond == 0


@pytest.mark.django_db
def test_ciphertext_length_does_not_depend_on_the_number_of_choices(world):
    w = world
    with transaction.atomic():
        cast_as(w, w["voters"][0], [])
        cast_as(w, w["voters"][1], ids(w, 0))
        cast_as(w, w["voters"][2], ids(w, 0, 1))
    assert {len(bytes(b.ciphertext)) for b in ballots(w["election"])} == {
        encoding.CIPHERTEXT_BYTES}


@pytest.mark.django_db
def test_web_role_cannot_read_or_write_the_ballot_box(world):
    w = world
    with transaction.atomic():
        cast_as(w, w["voters"][0], ids(w, 0))
    with transaction.atomic(), db_context(org_id=w["org"].id):
        for sql in ("SELECT ciphertext FROM ballot", "DELETE FROM ballot",
                    "UPDATE ballot SET receipt_commitment = receipt_commitment"):
            with pytest.raises(DatabaseError, match="permission denied"), \
                    transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(sql)
        # The voting role may insert only: it cannot read ballots back either.
        with as_db_role("app_voting"), pytest.raises(DatabaseError, match="permission denied"), \
                transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT ciphertext FROM ballot")
    with acting_as(None):  # even the owner cannot change or remove a ballot
        for sql in ("DELETE FROM ballot", "UPDATE ballot SET ciphertext = ciphertext"):
            with pytest.raises(DatabaseError, match="never be changed"), \
                    transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(sql)


@pytest.mark.django_db
def test_ballot_box_cannot_be_truncated():
    # No writes in this test: TRUNCATE refuses earlier if FK trigger events are pending.
    with acting_as(None), pytest.raises(DatabaseError, match="cannot be truncated"), \
            transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("TRUNCATE ballot")


# --- one person, one vote ------------------------------------------------------------------

@pytest.mark.django_db
def test_retry_with_same_key_is_answered_truthfully_and_never_counts_twice(world):
    w = world
    voter = w["voters"][0]
    with transaction.atomic():
        first, key = cast_as(w, voter, ids(w, 0))
        replay, _ = cast_as(w, voter, ids(w, 1), key=key)  # lost response, same form
        other, _ = cast_as(w, voter, ids(w, 1))  # another device / new form
    assert (first.status, replay.status, other.status) == ("accepted", "replayed",
                                                           "already_voted")
    assert len(ballots(w["election"])) == 1


@pytest.mark.django_db
def test_failure_before_commit_records_nothing_and_a_retry_succeeds(world):
    """Acceptance test 15: an interrupted cast is all-or-nothing."""
    w = world
    voter = w["voters"][0]
    with acting_as(None):
        entitlement = Entitlement.all_objects.get(election=w["election"], member=voter[0])
    with transaction.atomic():
        key = services.issue_idempotency_key(entitlement.id)
        with mock.patch("voting.services.audit", side_effect=RuntimeError("connection lost")):
            with pytest.raises(RuntimeError), transaction.atomic():
                cast_as(w, voter, ids(w, 0), key=key)
        assert ballots(w["election"]) == []
        assert entitlement_status(w, voter[0]) == "issued"
        retry, _ = cast_as(w, voter, ids(w, 0), key=key)
    assert retry.status == "accepted" and len(ballots(w["election"])) == 1


@pytest.mark.django_db
def test_forged_or_foreign_idempotency_keys_are_rejected(world):
    w = world
    a, b = w["voters"][0], w["voters"][1]
    with acting_as(None):
        b_entitlement = Entitlement.all_objects.get(election=w["election"], member=b[0])
    with transaction.atomic():
        with pytest.raises(BallotRejected, match="not valid"):
            cast_as(w, a, ids(w, 0), key=services.issue_idempotency_key(b_entitlement.id))
        with pytest.raises(BallotRejected, match="not valid"):
            cast_as(w, a, ids(w, 0), key="forged:key")


# --- eligibility, selections and the voting window ------------------------------------------

@pytest.mark.django_db
def test_members_outside_the_electorate_cannot_vote(world):
    """Acceptance test 3, service level (HTTP level below)."""
    w = world
    outsider = w["outsider"]
    late_user, _ = make_user("late@example.org")
    late = make_member(w["org"], "LATE-1", email=late_user.email, user=late_user)
    with transaction.atomic():
        for member, user in ((outsider[0], outsider[1]), (late, late_user)):
            with db_context(org_id=w["org"].id, user_id=user.id), \
                    pytest.raises(BallotRejected, match="not on the voter list"):
                services.cast(election_id=w["election"].id, member_id=member.id,
                              selections=[str(ids(w, 0)[0])], idempotency_key="x",
                              receipt_commitment=None, actor=user)
    assert ballots(w["election"]) == []


@pytest.mark.parametrize("make_selection,message", [
    (lambda w: ids(w, 0, 1, 2), "at most 2"),  # acceptance test 6
    (lambda w: [ids(w, 0)[0], ids(w, 0)[0]], "more than once"),
    (lambda w: [uuid.uuid4()], "unknown choice"),
    (lambda w: ["not-a-uuid"], "unknown choice"),
])
@pytest.mark.django_db
def test_invalid_selections_are_rejected_and_nothing_is_recorded(world, make_selection, message):
    w = world
    with transaction.atomic(), pytest.raises(BallotRejected, match=message):
        cast_as(w, w["voters"][0], make_selection(w))
    assert ballots(w["election"]) == []
    assert entitlement_status(w, w["voters"][0][0]) == "issued"


@pytest.mark.django_db
def test_partial_and_blank_ballot_rules(two_orgs):
    a, _ = two_orgs
    w = build_open_election(a.org, a.admin, seats=2, voters=2, rules_overrides={
        "partial_ballots_allowed": False, "blank_ballot_allowed": False, "min_selections": 2})
    with transaction.atomic():
        with pytest.raises(BallotRejected, match="at least one"):
            cast_as(w, w["voters"][0], [])
        with pytest.raises(BallotRejected, match="at least 2"):
            cast_as(w, w["voters"][0], ids(w, 0))
        assert cast_as(w, w["voters"][0], ids(w, 0, 1))[0].status == "accepted"


@pytest.mark.django_db
def test_votes_outside_the_voting_window_are_refused(world, two_orgs):
    """Acceptance test 5: after the closing time the service and the database refuse,
    even if the scheduler has not closed the election yet."""
    w = world
    backdate_closing(w["election"])
    with transaction.atomic(), pytest.raises(BallotRejected, match="not open"):
        cast_as(w, w["voters"][0], ids(w, 0))
    with transaction.atomic(), db_context(org_id=w["org"].id), as_db_role("app_voting"), \
            pytest.raises(DatabaseError, match="voting is not open"), transaction.atomic():
        Ballot.objects.create(organisation=w["org"], election=w["election"],
                              ciphertext=b"\0" * encoding.CIPHERTEXT_BYTES,
                              receipt_commitment=b"\0" * 32)
    # A scheduled (not yet open) election refuses too.
    a, _ = two_orgs
    scheduled = make_election(a.org, a.admin, title="Not yet open")
    with transaction.atomic(), db_context(org_id=a.org.id), \
            pytest.raises(BallotRejected, match="not open"):
        services.cast(election_id=scheduled.id, member_id=w["voters"][0][0].id, selections=[],
                      idempotency_key="x", receipt_commitment=None, actor=a.admin)


# --- closing ------------------------------------------------------------------------------

@pytest.mark.django_db
def test_closing_seals_the_ballot_box(world):
    w = world
    with transaction.atomic():
        cast_as(w, w["voters"][0], ids(w, 0))
        cast_as(w, w["voters"][1], ids(w, 1, 2))
    backdate_closing(w["election"])
    assert close_due_elections() == [(str(w["election"].id), "closed (2 ballots)")]
    with acting_as(None):
        election = Election.all_objects.get(pk=w["election"].pk)
        event = AuditEvent.objects.get(action="election.ballot_box_sealed")
    assert election.state == "closed"
    assert election.ballot_set_digest == encoding.ballot_set_digest(
        b.ciphertext for b in ballots(election))
    assert event.details["reconciled"] is True and event.details["ballots"] == 2
    with transaction.atomic(), pytest.raises(BallotRejected, match="not open"):
        cast_as(w, w["voters"][2], ids(w, 0))


# --- concurrency (acceptance test 4) -------------------------------------------------------

def _cleanup(org_id, user_ids):
    """Remove committed test data (triggers off: superuser-only replication role)."""
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute("SET session_replication_role = replica")
        cursor.execute("SELECT table_name FROM information_schema.columns "
                       "WHERE column_name = 'organisation_id' AND table_schema = 'public'")
        for (table,) in cursor.fetchall():
            cursor.execute(f'DELETE FROM "{table}" WHERE organisation_id = %s',  # noqa: S608
                           [org_id])
        cursor.execute("DELETE FROM organisation WHERE id = %s", [org_id])
        for table in ("otp_totp_totpdevice", "accounts_recovery_code_device"):
            cursor.execute(f'DELETE FROM "{table}" WHERE user_id = ANY(%s)',  # noqa: S608
                           [user_ids])
        cursor.execute("DELETE FROM app_user WHERE id = ANY(%s)", [user_ids])
        cursor.execute("SET session_replication_role = origin")
        cursor.execute("SET ROLE app_rw")


@pytest.fixture
def committed_world(django_db_setup, django_db_blocker):
    """Like ``world`` but committed, so separate connections can race each other."""
    with django_db_blocker.unblock():
        with acting_as(None):
            org = Organisation.objects.create(slug=f"race-{uuid.uuid4().hex[:6]}",
                                              legal_name="Race Club (fictional)")
        admin, _ = make_user(f"admin-{org.slug}@example.org")
        add_member(org, admin, Role.ORG_ADMIN)
        w = build_open_election(org, admin, voters=3)
        try:
            yield w
        finally:
            with acting_as(None):
                from accounts.models import User
                user_ids = [str(u) for u in User.objects.filter(
                    memberships__organisation=org).values_list("id", flat=True)]
            _cleanup(str(org.id), user_ids + [str(admin.id)])


def _race(w, attempts):
    """Run ``attempts`` (voter, key-or-None) concurrently, each on its own connection."""
    barrier = threading.Barrier(len(attempts))
    outcomes, errors = [], []
    # Each thread gets its own connection; give it the test database settings
    # (including the app_rw role) of the main thread's connection explicitly.
    main_settings = copy.deepcopy(connection.settings_dict)

    def worker(voter, key):
        try:
            connections["default"].settings_dict = copy.deepcopy(main_settings)
            barrier.wait()
            with transaction.atomic():
                outcomes.append(cast_as(w, voter, ids(w, 0), key=key)[0].status)
        except BallotRejected as exc:  # pragma: no cover - would be a test failure
            errors.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=a) for a in attempts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes, errors


def test_parallel_duplicate_submissions_produce_exactly_one_ballot(committed_world):
    """200 submissions for one voter (20 connections x 10 rounds), half re-using one
    form key and half with fresh keys: exactly one ballot, every other answer truthful."""
    w = committed_world
    voter = w["voters"][0]
    with acting_as(None):
        entitlement = Entitlement.all_objects.get(election=w["election"], member=voter[0])
    shared_key = services.issue_idempotency_key(entitlement.id)
    all_outcomes, all_errors = [], []
    for _ in range(10):
        attempts = [(voter, shared_key if i % 2 else None) for i in range(20)]
        outcomes, errors = _race(w, attempts)
        all_outcomes += outcomes
        all_errors += errors
    assert all_errors == []
    assert len(all_outcomes) == 200
    assert all_outcomes.count("accepted") == 1
    assert set(all_outcomes) <= {"accepted", "replayed", "already_voted"}
    with acting_as(None):
        assert Ballot.objects.filter(election=w["election"]).count() == 1


def test_parallel_votes_by_different_members_are_all_counted(committed_world):
    w = committed_world
    outcomes, errors = _race(w, [(v, None) for v in w["voters"]] * 4)
    assert errors == [] and outcomes.count("accepted") == 3
    with acting_as(None):
        assert Ballot.objects.filter(election=w["election"]).count() == 3


# --- through the web UI -------------------------------------------------------------------

@pytest.mark.django_db
def test_ui_vote_review_cast_receipt(client, world):
    """Acceptance test 7 (receipt reveals no choices) via the real pages."""
    w = world
    member, user, device = w["voters"][0]
    login(client, user, device)
    base = f"/o/{w['org'].slug}/elections/{w['election'].id}/ballot/"
    page = client.get(base).content.decode()
    assert "<strong>up to 2</strong>" in page and CANDIDATE_NAMES[0] in page
    r = client.post(f"{base}review/", {"candidate": [str(i) for i in ids(w, 0, 1, 2)]})
    assert r.status_code == 400 and "at most 2" in r.content.decode()
    r = client.post(f"{base}review/", {"candidate": [str(i) for i in ids(w, 1)]})
    review = r.content.decode()
    assert "cannot change your vote" in review
    key = re.search(r'name="idempotency_key" value="([^"]+)"', review).group(1)
    r = client.post(f"{base}cast/", {"candidate": [str(i) for i in ids(w, 1)],
                                     "idempotency_key": key, "receipt_commitment": ""})
    receipt = r.content.decode()
    assert r.status_code == 200 and "Your ballot was accepted" in receipt
    code = re.search(r'id="receipt-code"[^>]*>([0-9A-Z-]+)<', receipt).group(1)
    assert bytes(ballots(w["election"])[0].receipt_commitment) == encoding.receipt_commitment(
        w["election"].id, code)
    for name in CANDIDATE_NAMES:
        assert name not in receipt
    for candidate in w["candidates"]:
        assert str(candidate.id) not in receipt
    # Resubmitting the same form (e.g. after a timeout) is answered "accepted" again.
    r = client.post(f"{base}cast/", {"candidate": [str(i) for i in ids(w, 2)],
                                     "idempotency_key": key, "receipt_commitment": ""})
    assert r.status_code == 200 and "Your ballot was accepted" in r.content.decode()
    assert len(ballots(w["election"])) == 1
    assert "already been accepted" in client.get(base).content.decode()
    assert "Your ballot was accepted" in client.get(f"/o/{w['org'].slug}/").content.decode()


@pytest.mark.django_db
def test_browser_made_receipt_commitment_is_used(client, world):
    w = world
    _, user, device = w["voters"][0]
    login(client, user, device)
    base = f"/o/{w['org'].slug}/elections/{w['election'].id}/ballot/"
    review = client.post(f"{base}review/", {"candidate": [str(ids(w, 0)[0])]}).content.decode()
    key = re.search(r'name="idempotency_key" value="([^"]+)"', review).group(1)
    code = encoding.new_receipt_code()  # what ballot.js does in the browser
    commitment = hashlib.sha256(f"rcpt/v1{w['election'].id}{code}".encode()).hexdigest()
    r = client.post(f"{base}cast/", {"candidate": [str(ids(w, 0)[0])], "idempotency_key": key,
                                     "receipt_commitment": commitment})
    assert r.status_code == 200 and code not in r.content.decode()  # the server never saw it
    assert bytes(ballots(w["election"])[0].receipt_commitment).hex() == commitment


@pytest.mark.django_db
def test_ui_ineligible_member_is_refused(client, world):
    """Acceptance test 3: a manipulated request from a member outside the electorate."""
    w = world
    _, user, device = w["outsider"]
    login(client, user, device)
    base = f"/o/{w['org'].slug}/elections/{w['election'].id}/ballot/"
    assert client.get(base).status_code == 403
    r = client.post(f"{base}cast/", {"candidate": [str(ids(w, 0)[0])],
                                     "idempotency_key": "x"})
    assert r.status_code == 403 and ballots(w["election"]) == []


@pytest.mark.django_db
def test_no_choice_leaks_into_logs_audit_email_or_sessions(client, world, caplog):
    """Acceptance test 8: the canary candidate's name and id appear nowhere but the ballot."""
    w = world
    canary = w["candidates"][[c.display_name for c in w["candidates"]].index(
        CANDIDATE_NAMES[0])]
    _, user, device = w["voters"][0]
    login(client, user, device)
    base = f"/o/{w['org'].slug}/elections/{w['election'].id}/ballot/"
    with acting_as(None):  # audit events before voting legitimately name candidates
        before = set(AuditEvent.objects.values_list("id", flat=True))
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    mail.outbox.clear()
    review = client.post(f"{base}review/", {"candidate": [str(canary.id)]}).content.decode()
    key = re.search(r'name="idempotency_key" value="([^"]+)"', review).group(1)
    client.post(f"{base}cast/", {"candidate": [str(canary.id)], "idempotency_key": key})
    haystacks = [caplog.text] + [m.body + m.subject for m in mail.outbox]
    with acting_as(None), connection.cursor() as cursor:
        cursor.execute("SELECT id, action, details::text FROM audit_event")
        new_events = [f"{a} {d}" for i, a, d in cursor.fetchall() if i not in before]
        assert any(e.startswith("ballot.accepted") for e in new_events)
        haystacks += new_events
        cursor.execute("SELECT session_data FROM django_session")
        haystacks += [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT row_to_json(e)::text FROM entitlement e WHERE election_id = %s",
                       [w["election"].id])
        haystacks += [row[0] for row in cursor.fetchall()]
    blob = "\n".join(haystacks)
    assert "Canary-7Q4" not in blob and str(canary.id) not in blob
    assert len(ballots(w["election"])) == 1


@pytest.mark.django_db
def test_marked_register_shows_who_voted_but_never_how(client, world, two_orgs):
    w = world
    a, _ = two_orgs
    with transaction.atomic():
        cast_as(w, w["voters"][0], ids(w, 0))
    ro, ro_device = w["ro"]
    login(client, ro, ro_device)
    url = f"/o/{w['org'].slug}/elections/{w['election'].id}/marked-register/"
    page = client.get(url).content.decode()
    with acting_as(None):
        electorate = Entitlement.all_objects.filter(election=w["election"]).count()
    assert f"1 of {electorate} have voted" in page
    # Candidates appear only as voters (they are members); no candidate id, i.e. no
    # choice, appears anywhere on the page.
    for candidate in w["candidates"]:
        assert str(candidate.id) not in page
    admin_client = client.__class__()
    login(admin_client, a.admin, a.admin_device)
    assert admin_client.get(url).status_code == 403  # admins see turnout only (D6)
