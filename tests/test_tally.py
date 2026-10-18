"""Counting, ties, recounts, certification, publication and the receipt check.

Acceptance tests 9 (the tally matches an independently calculated result), 10
(partial ballots and abstentions are counted correctly), 11 (ties are flagged and
handled by the approved procedure), 13 (recounts preserve the original tally) and
14 (certified results cannot be silently altered).
"""

import base64
import hashlib
import itertools
import json
import random
import re
import uuid
from datetime import timedelta

import pytest
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.utils import timezone

from audit.models import AuditEvent
from elections import lifecycle
from elections import services as election_services
from elections.models import Election, LifecycleTransition
from electorate.services import close_due_elections
from keys import crypto
from platform_core.db import acting_as, db_context
from tally import counting, services
from tally.models import Certification, CountRequest, KeyContribution, Tally, TallyLine
from tenancy.roles import Role
from tests.factories import KEYHOLDER_PASSPHRASE, backdate_closing, valid_rules
from tests.helpers import login
from tests.test_voting import build_open_election, cast_as, ids
from voting import encoding
from voting.services import as_db_role

RO = {Role.RETURNING_OFFICER}
PASS = {"passphrase": KEYHOLDER_PASSPHRASE}


# --- pure counting -----------------------------------------------------------------------

def _reference(votes: dict, seats: int) -> dict:
    """Independent definition: elected if the candidates with at least as many votes fit in
    the seats; not elected if the candidates with more votes already fill them; else tied."""
    outcome = {}
    for c, v in votes.items():
        more = sum(1 for o in votes.values() if o > v)
        at_least = sum(1 for o in votes.values() if o >= v)
        outcome[c] = ("elected" if at_least <= seats or len(votes) <= seats
                      else "not_elected" if more >= seats else "tied_pending")
    return outcome


def test_allocation_matches_an_independent_reference_on_random_elections():
    rng = random.Random(20261017)  # noqa: S311 - reproducible test data
    for _ in range(3000):
        n = rng.randint(1, 8)
        seats = rng.randint(1, 5)
        votes = {uuid.uuid4(): rng.randint(0, 4) for _ in range(n)}
        lines = counting.allocate(list(votes.items()), seats)
        assert {line.candidate_id: line.outcome for line in lines} == _reference(votes, seats)
        assert [line.votes for line in lines] == sorted(votes.values(), reverse=True)
        for line in lines:
            assert line.rank == 1 + sum(1 for v in votes.values() if v > line.votes)
            assert (line.tie_group is not None) == (line.outcome == "tied_pending")
        elected = sum(1 for line in lines if line.outcome == "elected")
        tied = [line for line in lines if line.outcome == "tied_pending"]
        assert elected <= seats
        if tied:
            assert 0 < counting.seats_in_tie(lines, seats) < len(tied)
        else:
            assert elected == min(seats, n)


def test_count_matches_a_reference_tally_and_flags_invalid_ballots():
    key = crypto.PrivateKey.generate()
    public = bytes(key.public_key)
    election_id, other_election = uuid.uuid4(), uuid.uuid4()
    candidates = [uuid.uuid4() for _ in range(4)]
    rules = valid_rules(2)
    rng = random.Random(7)  # noqa: S311 - reproducible test data
    expected = dict.fromkeys(candidates, 0)
    ciphertexts, blank = [], 0
    for _ in range(200):
        chosen = rng.sample(candidates, rng.randint(0, 2))
        blank += not chosen
        for c in chosen:
            expected[c] += 1
        ciphertexts.append(encoding.seal(public, encoding.encode(election_id, chosen)))
    bad = [
        encoding.seal(public, encoding.encode(other_election, candidates[:1])),  # wrong election
        encoding.seal(public, encoding.encode(election_id, candidates[:3])),  # too many
        encoding.seal(public, encoding.encode(election_id, [uuid.uuid4()])),  # unknown
        encoding.seal(bytes(crypto.PrivateKey.generate().public_key),
                      encoding.encode(election_id, candidates[:1])),  # wrong key
        b"\0" * encoding.CIPHERTEXT_BYTES,  # garbage
    ]
    result = counting.count(key, ciphertexts + bad, election_id=election_id,
                            candidates=candidates, rules=rules, seats=2)
    assert result.ballots_total == 205 and result.ballots_valid == 200
    assert result.ballots_invalid == 5 and result.ballots_blank == blank
    assert {line.candidate_id: line.votes for line in result.lines} == expected


def test_any_two_of_four_holders_rebuild_the_key_through_staging():
    holders = [crypto.PrivateKey.generate() for _ in range(3)]
    result = crypto.ceremony([bytes(h.public_key) for h in holders])
    backup_secret = crypto.parse_backup_secret(result["backup_secret"])
    bundles = [crypto.open_holder_bundle(s, h) for s, h in zip(result["sealed_bundles"],
                                                               holders, strict=True)]
    bundles.append(crypto.open_backup_bundle(result["backup_bundle"], result["backup_secret"]))
    privates = [*holders, crypto.backup_private_key(backup_secret)]
    assert bytes(privates[3].public_key) == result["backup_public_key"]
    recipients = {i + 1: bytes(p.public_key) for i, p in enumerate(privates)}
    for a, b in itertools.permutations(range(4), 2):
        staged = crypto.stage_pieces(a + 1, bundles[a],
                                     {k: v for k, v in recipients.items() if k != a + 1})
        assert set(staged) == {str(i + 1) for i in range(4) if i != a}
        piece = base64.b64decode(staged[str(b + 1)])
        key = crypto.rebuild_from_staged(bundles[b], privates[b], piece, result["public_key"])
        assert bytes(key.public_key) == result["public_key"]
        # A piece staged for b is useless to anyone else.
        other = next(i for i in range(4) if i not in (a, b))
        with pytest.raises(crypto.KeyError_):
            crypto.rebuild_from_staged(bundles[other], privates[other], piece,
                                       result["public_key"])


# --- the counting workflow ------------------------------------------------------------------

@pytest.fixture
def world(db, two_orgs):
    a, _ = two_orgs
    w = build_open_election(a.org, a.admin)
    w["admin"] = a.admin
    return w


def _vote_and_close(w, choices):
    with transaction.atomic():
        for voter, positions in zip(w["voters"], choices, strict=False):
            cast_as(w, voter, ids(w, *positions))
    backdate_closing(w["election"])
    assert close_due_elections()[0][1].startswith("closed")


def _as(w, user):
    return db_context(org_id=w["org"].id, user_id=user.id)


def _election(w):
    with acting_as(None):
        return Election.all_objects.select_related("current_rule_version",
                                                   "organisation").get(pk=w["election"].pk)


def _start(w, reason=""):
    ro = w["ro"][0]
    with transaction.atomic(), _as(w, ro):
        return services.start_count(Election.objects.get(pk=w["election"].pk), actor=ro,
                                    reason=reason)


def _give(w, request, user, **kwargs):
    with transaction.atomic(), _as(w, user):
        return services.contribute(request, actor=user, **kwargs)


def _count(w, reason="", second=None):
    """Start a count; keyholder 2 stages, keyholder 3 (or ``second``) completes."""
    request = _start(w, reason)
    _, k2, k3 = w["keyholders"]
    assert _give(w, request, k2, **PASS).status == "staged"
    user, kwargs = second or (k3, PASS)
    result = _give(w, request, user, **kwargs)
    assert result.status == "counted"
    return result.tally


def _lines(tally):
    with acting_as(None):
        return {line.candidate_id: (line.votes, line.outcome)
                for line in TallyLine.all_objects.filter(tally=tally)}


def _certify(w, user=None):
    user = user or w["ro"][0]
    with transaction.atomic(), _as(w, user):
        return services.certify(_in_org(w), actor=user)


def _in_org(w):
    """The election, read in the current organisation context."""
    return Election.objects.select_related("current_rule_version",
                                           "organisation").get(pk=w["election"].pk)


@pytest.mark.django_db
def test_two_keyholders_count_and_the_result_is_certified_and_published(world):
    w = world
    _vote_and_close(w, [(0,), (0, 1), (1, 2)])
    tally = _count(w)
    c0, c1, c2 = ids(w, 0, 1, 2)
    assert _lines(tally) == {c0: (2, "elected"), c1: (2, "elected"), c2: (1, "not_elected")}
    assert (tally.ballots_total, tally.ballots_valid, tally.entitlements_consumed) == (3, 3, 3)
    assert tally.reconciliation_ok and not tally.has_ties and tally.version == 1
    assert _election(w).state == lifecycle.AWAITING
    with acting_as(None):
        assert not KeyContribution.all_objects.exists()  # staged pieces are gone
        assert CountRequest.all_objects.get().status == "completed"
        transitions = list(LifecycleTransition.all_objects.filter(election_id=w["election"].pk)
                           .order_by("at").values_list("to_state", "actor_id"))
    assert transitions[-2:] == [("counted", w["ro"][0].id), ("awaiting_certification", None)]

    # Nothing is visible to members before publication (acceptance test 9).
    with transaction.atomic(), _as(w, w["voters"][0][1]):
        assert services.published_result(_in_org(w)) is None

    certification = _certify(w)
    document = json.loads(certification.document)
    assert certification.content_sha256 == hashlib.sha256(
        certification.document.encode()).hexdigest()
    assert document["count"]["ballots"] == 3
    assert [r["outcome"] for r in document["results"]] == ["elected", "elected", "not_elected"]
    assert document["receipts"]["count"] == 3
    assert _election(w).state == lifecycle.CERTIFIED
    with transaction.atomic(), _as(w, w["voters"][0][1]):
        assert services.published_result(_in_org(w)) is None
    with transaction.atomic(), _as(w, w["admin"]):
        services.publish(_in_org(w), actor=w["admin"])
        with pytest.raises(services.TallyError, match="already published"):
            services.publish(_in_org(w), actor=w["admin"])
    with transaction.atomic(), _as(w, w["voters"][0][1]):
        assert services.published_result(_in_org(w)).pk == certification.pk


@pytest.mark.django_db
def test_one_keyholder_alone_cannot_count_and_people_must_differ(world):
    w = world
    _vote_and_close(w, [(0,)])
    request = _start(w)
    ro, k2, _ = w["keyholders"]
    assert _give(w, request, ro, **PASS).status == "staged"
    with pytest.raises(services.TallyError, match="already provided"):
        _give(w, request, ro, **PASS)
    # The same person cannot add the backup code as a second share either.
    with pytest.raises(services.TallyError, match="already provided"):
        _give(w, request, ro, backup_code=w["backup_code"])
    # A wrong passphrase changes nothing; the staged share is kept.
    with pytest.raises(services.TallyError, match="passphrase is not correct"):
        _give(w, request, k2, passphrase="not the right passphrase at all")
    with acting_as(None):
        assert KeyContribution.all_objects.count() == 1 and not Tally.all_objects.exists()
    # Someone who is not a keyholder can only offer the backup code.
    with pytest.raises(services.TallyError, match="not a keyholder"):
        _give(w, request, w["admin"], **PASS)
    assert _give(w, request, w["admin"], backup_code=w["backup_code"]).status == "counted"


@pytest.mark.django_db
def test_the_server_alone_cannot_rebuild_the_key_from_what_it_stores(world):
    """After the first contribution the server stores only pieces sealed to other holders."""
    w = world
    _vote_and_close(w, [(0,)])
    request = _start(w)
    _give(w, request, w["keyholders"][1], **PASS)
    with acting_as(None):
        staged = KeyContribution.all_objects.get()
    # Opening a staged piece needs that holder's private key (passphrase or backup code),
    # and the key never appears in anything stored.
    for piece in staged.sealed_pieces.values():
        with pytest.raises(crypto.nacl.exceptions.CryptoError):
            crypto.SealedBox(crypto.PrivateKey.generate()).decrypt(base64.b64decode(piece))
    assert set(staged.sealed_pieces) == {"1", "3", "4"}  # one piece for each other holder
    assert staged.expires_at <= timezone.now() + services.CONTRIBUTION_TTL


@pytest.mark.django_db
def test_backup_code_can_be_either_share(world):
    w = world
    _vote_and_close(w, [(1,)])
    request = _start(w)
    assert _give(w, request, w["admin"], backup_code=w["backup_code"]).status == "staged"
    result = _give(w, request, w["keyholders"][2], **PASS)
    assert result.status == "counted" and _lines(result.tally)[ids(w, 1)[0]][0] == 1


@pytest.mark.django_db
def test_expired_staged_shares_are_ignored_and_purged(world):
    w = world
    _vote_and_close(w, [(0,)])
    request = _start(w)
    _give(w, request, w["keyholders"][1], **PASS)
    with acting_as(None):
        KeyContribution.all_objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    assert services.purge_expired_contributions() == 1
    with acting_as(None):
        assert not KeyContribution.all_objects.exists()
        assert AuditEvent.objects.filter(action="count.share_expired").count() == 1
    # The next contribution starts again from the beginning.
    assert _give(w, request, w["keyholders"][2], **PASS).status == "staged"


@pytest.mark.django_db
def test_tie_for_the_last_seat_must_be_resolved_before_certification(world):
    w = world
    _vote_and_close(w, [(0, 1), (0, 2), (0,)])
    tally = _count(w)
    c0, c1, c2 = ids(w, 0, 1, 2)
    assert _lines(tally) == {c0: (3, "elected"), c1: (1, "tied_pending"),
                             c2: (1, "tied_pending")}
    assert tally.has_ties and _election(w).state == lifecycle.COUNTED
    ro = w["ro"][0]
    with transaction.atomic(), _as(w, ro):
        assert "tie" in " ".join(services.certification_blockers(_in_org(w), tally))
        with pytest.raises(services.TallyError, match="exactly 1"):
            services.resolve_tie(_in_org(w), actor=ro, elected_ids=[str(c1), str(c2)],
                                 evidence="Lots drawn at the AGM, witnessed by the auditor.")
        with pytest.raises(services.TallyError, match="exactly 1"):
            services.resolve_tie(_in_org(w), actor=ro, elected_ids=[str(c0)],
                                 evidence="Lots drawn at the AGM, witnessed by the auditor.")
        services.resolve_tie(_in_org(w), actor=ro, elected_ids=[str(c2)],
                             evidence="Lots drawn at the AGM, witnessed by the auditor.")
    assert _election(w).state == lifecycle.AWAITING
    document = json.loads(_certify(w).document)
    outcomes = {r["candidate_id"]: r["outcome"] for r in document["results"]}
    assert outcomes == {str(c0): "elected", str(c1): "not_elected", str(c2): "elected"}
    assert document["tie_resolution"]["elected"] == [w["candidates"][2].display_name]


@pytest.mark.django_db
def test_database_checks_tie_resolutions_independently(world):
    w = world
    _vote_and_close(w, [(0, 1), (0, 2), (0,)])
    tally = _count(w)
    c0, c1, c2 = ids(w, 0, 1, 2)
    from tally.models import TieResolution

    def insert(elected, not_elected, procedure="lot_drawn_by_returning_officer"):
        with transaction.atomic(), _as(w, w["ro"][0]), transaction.atomic():
            TieResolution.objects.create(
                organisation_id=w["org"].id, tally=tally, tie_group=1, procedure=procedure,
                procedure_description="x", elected=elected, not_elected=not_elected,
                evidence="evidence text", recorded_by=w["ro"][0])

    for bad in ([[str(c1), str(c2)], []], [[str(c0)], [str(c1)]], [[str(c1)], []]):
        with pytest.raises(DatabaseError, match="every tied candidate"):
            insert(*bad)
    with pytest.raises(DatabaseError, match="approved rules"):
        insert([str(c1)], [str(c2)], procedure="chair_casting_vote")


@pytest.mark.django_db
def test_recount_keeps_earlier_counts_and_needs_a_reason(world):
    w = world
    _vote_and_close(w, [(0,), (1,), (1,)])
    first = _count(w)
    with pytest.raises(services.TallyError, match="needs a reason"):
        _start(w)
    second = _count(w, reason="Candidate B asked for a recount (letter of 2 Nov)")
    assert (first.version, second.version, second.kind) == (1, 2, "recount")
    assert _lines(first) == _lines(second)
    with acting_as(None):
        assert Tally.all_objects.count() == 2
        history = list(LifecycleTransition.all_objects.filter(election_id=w["election"].pk)
                       .order_by("at").values_list("from_state", "to_state", "reason"))
    assert history[-2][:2] == ("awaiting_certification", "counted")
    assert history[-2][2].startswith("Candidate B asked")
    assert _election(w).state == lifecycle.AWAITING
    assert json.loads(_certify(w).document)["count"]["version"] == 2


@pytest.mark.django_db
def test_dispute_recount_supersedes_the_published_result(world, client):
    w = world
    _vote_and_close(w, [(0,), (1,), (1,)])
    _count(w)
    original = _certify(w)
    with transaction.atomic(), _as(w, w["admin"]):
        services.publish(_in_org(w), actor=w["admin"])
        election_services.transition(_in_org(w), lifecycle.DISPUTED, actor=w["admin"],
                                     roles={Role.ORG_ADMIN}, reason="Complaint from a member")
    # Re-certifying without a recount confirms the same certification.
    with transaction.atomic(), _as(w, w["ro"][0]):
        election_services.transition(_in_org(w), lifecycle.CERTIFIED, actor=w["ro"][0],
                                     roles=RO)
        election_services.transition(_in_org(w), lifecycle.DISPUTED, actor=w["ro"][0],
                                     roles=RO, reason="Second complaint")
    _count(w, reason="Recount ordered after the second complaint")
    with acting_as(None):
        original.refresh_from_db()
    assert original.superseded_at is not None
    assert original.supersede_reason.startswith("Recount ordered")
    member, user, device = w["voters"][0]
    login(client, user, device)
    page = client.get(f"/o/{w['org'].slug}/elections/{w['election'].id}/results/")
    assert "withdrawn" in page.content.decode()
    # Re-certifying is no longer possible: only the new count can be certified.
    with transaction.atomic(), _as(w, w["ro"][0]), \
            pytest.raises(DatabaseError, match="certification of the latest count"), \
            transaction.atomic():
        Election.objects.filter(pk=w["election"].pk).update(state="certified")
    renewed = _certify(w)
    assert json.loads(renewed.document)["count"]["version"] == 2


@pytest.mark.django_db
def test_reconciliation_failure_blocks_certification(world):
    """An entitlement marked as used without a ballot (simulated tampering)."""
    w = world
    _vote_and_close(w, [(0,), (1,)])
    with transaction.atomic(), acting_as(None), connection.cursor() as cursor:
        cursor.execute("SET LOCAL session_replication_role = replica")
        cursor.execute("UPDATE entitlement SET status = 'consumed', consumed_on = current_date, "
                       "idempotency_hash = %s WHERE member_id = %s",
                       [b"\0" * 32, w["voters"][2][0].id])
        cursor.execute("SET LOCAL session_replication_role = origin")
    tally = _count(w)
    assert not tally.reconciliation_ok
    assert tally.reconciliation["ballots_equal_voters_marked"] is False
    assert _election(w).state == lifecycle.COUNTED
    with pytest.raises(services.TallyError, match="Only an election awaiting"):
        _certify(w)
    with transaction.atomic(), _as(w, w["ro"][0]), \
            pytest.raises(DatabaseError, match="reconciled count"), transaction.atomic():
        Election.objects.filter(pk=w["election"].pk).update(state="awaiting_certification")


@pytest.mark.django_db
def test_an_invalid_ballot_in_the_box_is_flagged(world):
    """A ballot slipped into the box (bypassing the casting service) for another election."""
    w = world
    with transaction.atomic():
        cast_as(w, w["voters"][0], ids(w, 0))
    election = _election(w)
    forged = encoding.seal(bytes(election.public_key),
                           encoding.encode(uuid.uuid4(), ids(w, 0)))
    with transaction.atomic(), acting_as(None), connection.cursor() as cursor:
        cursor.execute("INSERT INTO ballot (id, organisation_id, election_id, ciphertext, "
                       "receipt_commitment) VALUES (gen_random_uuid(), %s, %s, %s, %s)",
                       [w["org"].id, election.id, forged, hashlib.sha256(b"x").digest()])
        cursor.execute("SET LOCAL session_replication_role = replica")
        cursor.execute("UPDATE entitlement SET status = 'consumed', consumed_on = current_date, "
                       "idempotency_hash = %s WHERE member_id = %s",
                       [b"\0" * 32, w["voters"][1][0].id])
        cursor.execute("SET LOCAL session_replication_role = origin")
    backdate_closing(w["election"])
    close_due_elections()
    tally = _count(w)
    assert (tally.ballots_total, tally.ballots_invalid) == (2, 1)
    assert not tally.reconciliation_ok and not tally.reconciliation["all_ballots_valid"]
    assert _lines(tally)[ids(w, 0)[0]][0] == 1


@pytest.mark.django_db
def test_the_rules_author_cannot_certify(world):
    w = world
    _vote_and_close(w, [(0, 1)])
    _count(w)
    with pytest.raises(services.TallyError, match="wrote the election rules"):
        _certify(w, user=w["admin"])
    # The database refuses it too, independently of the service.
    with acting_as(None):
        tally = Tally.all_objects.get()
    with transaction.atomic(), _as(w, w["admin"]), \
            pytest.raises(DatabaseError, match="wrote the rules"), transaction.atomic():
        Certification.objects.create(organisation_id=w["org"].id, election_id=tally.election_id,
                                     tally=tally, certified_by=w["admin"], statement="s",
                                     document="{}")


@pytest.mark.django_db
def test_tallies_and_certifications_are_immutable(world):
    w = world
    _vote_and_close(w, [(0, 1)])
    tally = _count(w)
    certification = _certify(w)
    with acting_as(None):
        line = TallyLine.all_objects.filter(tally=tally).first()
    attempts = [
        ("UPDATE tally SET ballots_total = 99 WHERE id = %s", [tally.id]),
        ("DELETE FROM tally_line WHERE id = %s", [line.id]),
        ("UPDATE tally_line SET votes = 99 WHERE id = %s", [line.id]),
        ("UPDATE certification SET document = '{}' WHERE id = %s", [certification.id]),
        ("DELETE FROM certification WHERE id = %s", [certification.id]),
        ("UPDATE certification SET superseded_at = now(), supersede_reason = 'x' "
         "WHERE id = %s", [certification.id]),  # only while disputed
    ]
    for sql, params in attempts:  # even the superuser is stopped by the triggers
        with acting_as(None), pytest.raises(DatabaseError), transaction.atomic(), \
                connection.cursor() as cursor:
            cursor.execute(sql, params)


@pytest.mark.django_db
def test_web_role_cannot_write_tallies_and_tally_role_cannot_read_receipts(world):
    w = world
    _vote_and_close(w, [(0,)])
    with transaction.atomic(), _as(w, w["ro"][0]):
        for sql in ("INSERT INTO tally_line (id) VALUES (gen_random_uuid())",
                    "INSERT INTO tally (id) VALUES (gen_random_uuid())",
                    "SELECT ciphertext FROM ballot"):
            with pytest.raises(DatabaseError, match="permission denied"), \
                    transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(sql)
        with as_db_role("app_tally"), pytest.raises(DatabaseError, match="permission denied"), \
                transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT receipt_commitment FROM ballot")


@pytest.mark.django_db
def test_a_tally_must_match_the_sealed_ballot_box(world):
    w = world
    _vote_and_close(w, [(0,), (1,)])
    election = _election(w)
    base = dict(organisation_id=w["org"].id, election=election, version=1, kind="original",
                ballots_valid=2, ballots_invalid=0, ballots_blank=0, entitlements_consumed=2,
                electorate_size=3, reconciliation={}, reconciliation_ok=True, has_ties=False,
                run_by=w["ro"][0])
    for overrides, message in [
        ({"ballot_set_digest": "0" * 64, "ballots_total": 2}, "digest sealed at close"),
        ({"ballot_set_digest": election.ballot_set_digest, "ballots_total": 1,
          "ballots_valid": 1}, "every ballot"),
        ({"ballot_set_digest": election.ballot_set_digest, "ballots_total": 2, "version": 2,
          "kind": "recount", "reason": "r"}, "consecutive"),
    ]:
        with transaction.atomic(), _as(w, w["ro"][0]), as_db_role("app_tally"), \
                pytest.raises(DatabaseError, match=message), transaction.atomic():
            Tally.objects.create(**{**base, **overrides})


@pytest.mark.django_db
def test_counting_never_records_how_anyone_voted(world, caplog):
    w = world
    _vote_and_close(w, [(0,), (1,)])
    with caplog.at_level("DEBUG"):
        _count(w)
    with acting_as(None):
        details = json.dumps(list(AuditEvent.objects.filter(action__startswith="tally.")
                                  .values_list("details", flat=True)))
    for candidate in w["candidates"]:
        assert str(candidate.id) not in details and str(candidate.id) not in caplog.text
    assert KEYHOLDER_PASSPHRASE not in caplog.text


# --- through the web UI ------------------------------------------------------------------

@pytest.mark.django_db
def test_ui_count_certify_publish_and_check_receipt(client, world):
    w = world
    with transaction.atomic():
        result, _ = cast_as(w, w["voters"][0], ids(w, 0))
    # The browser-made receipt flow is covered in test_voting; use a server-made one here.
    code = result.receipt_code
    with transaction.atomic():
        cast_as(w, w["voters"][1], ids(w, 1))
    backdate_closing(w["election"])
    close_due_elections()
    base = f"/o/{w['org'].slug}/elections/{w['election'].id}"

    member = client  # one signed-in client per person (each TOTP code is single-use)
    login(member, w["voters"][2][1], w["voters"][2][2])
    assert member.get(f"{base}/count/").status_code == 403
    assert "once they are certified" in member.get(f"{base}/results/").content.decode()
    receipts = member.get(f"{base}/receipts/").content.decode()
    commitment = encoding.receipt_commitment(w["election"].id, code).hex()
    assert commitment in receipts and code not in receipts

    ro_client, admin_client = Client(), Client()
    login(ro_client, *w["ro"])
    assert ro_client.post(f"{base}/count/start/", {"reason": ""}).status_code == 302
    r = ro_client.post(f"{base}/count/contribute/", {"passphrase": KEYHOLDER_PASSPHRASE,
                                                     "backup_code": ""})
    assert r.status_code == 302
    page = ro_client.get(f"{base}/count/").content.decode()
    assert "provided" in page and "Waiting for a second person" in page

    login(admin_client, w["admin"], world_admin_device(w))
    r = admin_client.post(f"{base}/count/contribute/",
                          {"passphrase": "", "backup_code": w["backup_code"]}, follow=True)
    assert "Counted (version 1)" in r.content.decode()

    page = ro_client.get(f"{base}/count/").content.decode()
    assert "Review and certify" in page and w["candidates"][0].display_name in page
    assert "Certify this result" in ro_client.get(f"{base}/certify/").content.decode()
    assert ro_client.post(f"{base}/certify/").status_code == 302
    assert member.get(f"{base}/results/document.json").status_code == 302  # not published

    assert "Publish" in admin_client.get(f"{base}/publish/").content.decode()
    assert admin_client.post(f"{base}/publish/").status_code == 302

    page = member.get(f"{base}/results/").content.decode()
    assert "Elected" in page and w["candidates"][0].display_name in page
    document = member.get(f"{base}/results/document.json")
    fingerprint = re.search(r"fingerprint \(SHA-256\): <code>([0-9a-f]{64})", page).group(1)
    assert hashlib.sha256(document.content).hexdigest() == fingerprint
    assert "attachment" in document.headers["Content-Disposition"]
    assert "Results" in member.get(f"/o/{w['org'].slug}/").content.decode()


def world_admin_device(w):
    from django_otp.plugins.otp_totp.models import TOTPDevice

    with acting_as(None):
        return TOTPDevice.objects.get(user=w["admin"])


@pytest.mark.django_db
def test_passphrases_never_reach_logs(client, world, caplog):
    w = world
    _vote_and_close(w, [(0,)])
    _start(w)
    ro, ro_device = w["ro"]
    login(client, ro, ro_device)
    with caplog.at_level("DEBUG"):
        client.post(f"/o/{w['org'].slug}/elections/{w['election'].id}/count/contribute/",
                    {"passphrase": "wrong passphrase that must not be logged", "backup_code": ""})
    assert "must not be logged" not in caplog.text
