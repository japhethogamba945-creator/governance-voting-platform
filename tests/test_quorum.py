"""Quorum assessment and the attendance register."""

import json

import pytest
from django.db import DatabaseError, transaction
from django.test import Client

from elections import lifecycle
from quorum import services
from quorum.models import ParticipationRecord
from tests.factories import make_member
from tests.helpers import login
from tests.test_tally import _as, _certify, _count, _election, _vote_and_close
from tests.test_voting import build_open_election

pytestmark = pytest.mark.django_db


def test_assessment_rules():
    absolute = {"type": "absolute", "basis": "accepted_ballots", "threshold": 3}
    assert services.assess(absolute, electorate=10, ballots=3, attendance=0)["met"]
    assert not services.assess(absolute, electorate=10, ballots=2, attendance=9)["met"]
    percent = {"type": "percent_of_electorate", "basis": "accepted_ballots", "threshold": 25}
    outcome = services.assess(percent, electorate=10, ballots=2, attendance=0)
    assert (outcome["required"], outcome["met"]) == (3, False)  # 25% of 10, rounded up
    assert services.assess({**percent, "threshold": 33.4}, electorate=3, ballots=2,
                           attendance=0)["required"] == 2
    attendance = {"type": "absolute", "basis": "registered_attendance", "threshold": 2}
    assert services.assess(attendance, electorate=10, ballots=0, attendance=2)["met"]
    assert services.assess({"type": "none"}, electorate=10, ballots=0, attendance=0)["met"]
    assert "NOT met" in services.assess(absolute, electorate=10, ballots=0, attendance=0)["text"]


def _world(two_orgs, quorum):
    a, _ = two_orgs
    w = build_open_election(a.org, a.admin, rules_overrides={"quorum": quorum})
    w["admin"] = a.admin
    return w


def test_quorum_outcome_is_reported_in_the_certified_result(two_orgs):
    """A8: the system reports quorum met / not met; it does not block or decide."""
    w = _world(two_orgs, {"type": "absolute", "basis": "accepted_ballots", "threshold": 3})
    _vote_and_close(w, [(0, 1), (0, 1)])
    _count(w)
    document = json.loads(_certify(w).document)
    assert document["quorum"]["met"] is False and document["quorum"]["required"] == 3
    assert document["quorum"]["participants"] == 2


def test_attendance_register_counts_towards_quorum(two_orgs):
    w = _world(two_orgs, {"type": "absolute", "basis": "registered_attendance",
                          "threshold": 2})
    ro = w["ro"][0]
    outsider = w["outsider"][0]
    numbers = [v[0].member_number for v in w["voters"][:2]]
    with transaction.atomic(), _as(w, ro):
        added, unknown = services.record_attendance(_election(w), numbers
                                                    + [outsider.member_number, "NOPE"],
                                                    actor=ro)
        assert (added, unknown) == (2, [outsider.member_number, "NOPE"])
        assert services.record_attendance(_election(w), numbers, actor=ro)[0] == 0
    _vote_and_close(w, [(0, 1)])
    _count(w)
    document = json.loads(_certify(w).document)
    assert document["quorum"]["met"] and document["quorum"]["participants"] == 2
    # Frozen after certification, by the service and by the database.
    with transaction.atomic(), _as(w, ro):
        with pytest.raises(services.QuorumError):
            services.record_attendance(_election(w), [w["voters"][2][0].member_number],
                                       actor=ro)
        record = ParticipationRecord.objects.first()
        with pytest.raises(DatabaseError, match="between opening and certification"), \
                transaction.atomic():
            ParticipationRecord.objects.filter(pk=record.pk).delete()


def test_database_accepts_attendance_only_for_the_electorate_while_open(two_orgs):
    w = _world(two_orgs, {"type": "absolute", "basis": "registered_attendance",
                          "threshold": 1})
    ro = w["ro"][0]
    late = make_member(w["org"], "LATE-1", full_name="Late Joiner (fictional)")
    with transaction.atomic(), _as(w, ro), \
            pytest.raises(DatabaseError, match="members of the electorate"), \
            transaction.atomic():
        ParticipationRecord.objects.create(organisation_id=w["org"].id,
                                           election=_election(w), member=late, recorded_by=ro)
    assert _election(w).state == lifecycle.OPEN


def test_attendance_page(two_orgs):
    w = _world(two_orgs, {"type": "absolute", "basis": "registered_attendance",
                          "threshold": 1})
    client = Client()
    login(client, *w["ro"])
    url = f"/o/{w['org'].slug}/elections/{w['election'].id}/attendance/"
    assert "Attendance register" in client.get(url).content.decode()
    number = w["voters"][0][0].member_number
    page = client.post(f"{url}add/", {"member_numbers": f"{number}, X-UNKNOWN"},
                       follow=True).content.decode()
    assert "1 attendance record added" in page and "X-UNKNOWN" in page
    with transaction.atomic(), _as(w, w["ro"][0]):
        record_id = ParticipationRecord.objects.get().id
    page = client.post(f"{url}remove/", {"record": str(record_id)}, follow=True).content.decode()
    assert "removed" in page
    with transaction.atomic(), _as(w, w["ro"][0]):
        assert not ParticipationRecord.objects.exists()
    # Members cannot see the register.
    member = Client()
    login(member, w["voters"][1][1], w["voters"][1][2])
    assert member.get(url).status_code == 403
