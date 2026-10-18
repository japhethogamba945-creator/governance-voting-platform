"""Phase 7: concurrency stress and fault injection on real, separate connections.

docs/design/05-security-review.md §5 lists these tests: 1,000 parallel
duplicate submissions, a connection killed before COMMIT, a lost response after
COMMIT, and submissions racing the closing time.
"""

import copy
import threading
import time
import uuid

import pytest
from django.db import (
    DatabaseError,
    InterfaceError,
    OperationalError,
    connection,
    connections,
    transaction,
)

from electorate.models import Entitlement
from electorate.services import close_due_elections
from platform_core.db import acting_as
from tenancy.models import Organisation
from tenancy.roles import Role
from tests.factories import add_member, make_user
from tests.test_voting import _cleanup, _race, build_open_election, cast_as, ids
from voting import encoding, services
from voting.models import Ballot
from voting.services import BallotRejected


def _committed(django_db_blocker, voters):
    """An open election committed to the database (separate connections can see it)."""
    from accounts.models import User

    with django_db_blocker.unblock():
        with acting_as(None):
            org = Organisation.objects.create(slug=f"stress-{uuid.uuid4().hex[:6]}",
                                              legal_name="Stress Club (fictional)")
        admin, _ = make_user(f"admin-{org.slug}@example.org")
        add_member(org, admin, Role.ORG_ADMIN)
        try:
            yield build_open_election(org, admin, voters=voters)
        finally:
            with acting_as(None):
                user_ids = [str(u) for u in User.objects.filter(
                    memberships__organisation=org).values_list("id", flat=True)]
            _cleanup(str(org.id), user_ids + [str(admin.id)])


@pytest.fixture
def stress_world(django_db_setup, django_db_blocker):
    yield from _committed(django_db_blocker, voters=3)


@pytest.fixture
def crowd_world(django_db_setup, django_db_blocker):
    yield from _committed(django_db_blocker, voters=40)


def _ballots(election):
    with acting_as(None):
        return list(Ballot.objects.filter(election=election))


def _entitlement(w, voter):
    with acting_as(None):
        return Entitlement.all_objects.get(election=w["election"], member=voter[0])


def test_1000_parallel_duplicate_submissions_produce_exactly_one_ballot(stress_world):
    """1,000 submissions for one voter: 50 connections at once, 20 rounds; half re-use one
    form key, half bring fresh keys. Exactly one ballot; every other answer truthful."""
    w = stress_world
    voter = w["voters"][0]
    shared_key = services.issue_idempotency_key(_entitlement(w, voter).id)
    outcomes, errors = [], []
    for _ in range(20):
        round_outcomes, round_errors = _race(
            w, [(voter, shared_key if i % 2 else None) for i in range(50)])
        outcomes += round_outcomes
        errors += round_errors
    assert errors == []
    assert len(outcomes) == 1000
    assert outcomes.count("accepted") == 1
    assert set(outcomes) <= {"accepted", "replayed", "already_voted"}
    assert len(_ballots(w["election"])) == 1
    assert _entitlement(w, voter).status == "consumed"


def test_connection_killed_before_commit_records_nothing_and_a_retry_succeeds(stress_world):
    """The database connection is terminated after the ballot is inserted but before COMMIT."""
    w = stress_world
    voter = w["voters"][0]
    key = services.issue_idempotency_key(_entitlement(w, voter).id)
    main_settings = copy.deepcopy(connection.settings_dict)
    ready, killed = threading.Event(), threading.Event()
    state = {}

    def doomed():
        connections["default"].settings_dict = copy.deepcopy(main_settings)
        try:
            with transaction.atomic():
                result, _ = cast_as(w, voter, ids(w, 0), key=key)
                state["status_before_commit"] = result.status
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    state["pid"] = cursor.fetchone()[0]
                ready.set()
                killed.wait(10)
        except (OperationalError, InterfaceError, DatabaseError) as exc:
            state["error"] = type(exc).__name__
        finally:
            connections.close_all()

    thread = threading.Thread(target=doomed)
    thread.start()
    assert ready.wait(10)
    with acting_as(None), connection.cursor() as cursor:
        cursor.execute("SELECT pg_terminate_backend(%s)", [state["pid"]])
        assert cursor.fetchone()[0] is True
    killed.set()
    thread.join(10)
    assert state["status_before_commit"] == "accepted"  # what the server had computed...
    assert "error" in state  # ...but the commit never happened, so no success was sent
    assert _ballots(w["election"]) == []
    assert _entitlement(w, voter).status == "issued"
    # The browser retries with the same key: accepted exactly once.
    outcomes, errors = _race(w, [(voter, key)])
    assert (outcomes, errors) == (["accepted"], [])
    assert len(_ballots(w["election"])) == 1


def test_response_lost_after_commit_retry_is_answered_truthfully(stress_world):
    """COMMIT succeeds but the response never reaches the voter; the retry says accepted."""
    w = stress_world
    voter = w["voters"][1]
    key = services.issue_idempotency_key(_entitlement(w, voter).id)
    first, _ = _race(w, [(voter, key)])
    retries, errors = _race(w, [(voter, key)] * 50)
    assert first == ["accepted"] and errors == []
    assert set(retries) == {"replayed"}  # the view answers "accepted" for a replay
    assert len(_ballots(w["election"])) == 1


def test_submissions_racing_the_closing_time(crowd_world):
    """40 voters submit in waves across the closing time while the scheduler closes the
    election. Every ballot was accepted before closing; none after the box was sealed;
    ballots equal consumed entitlements; the sealed digest covers exactly the ballots."""
    w = crowd_world
    election = w["election"]
    with transaction.atomic(), acting_as(None), connection.cursor() as cursor:
        cursor.execute("SET LOCAL session_replication_role = replica")
        cursor.execute("UPDATE election SET closes_at = now() + interval '1.5 seconds' "
                       "WHERE id = %s", [election.pk])
        cursor.execute("SET LOCAL session_replication_role = origin")
    main_settings = copy.deepcopy(connection.settings_dict)
    accepted, refused, errors = [], [], []

    def voter_thread(voter, delay):
        connections["default"].settings_dict = copy.deepcopy(main_settings)
        time.sleep(delay)
        try:
            with transaction.atomic():
                result, _ = cast_as(w, voter, ids(w, 1))
            accepted.append(result.status)
        except BallotRejected as exc:
            refused.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
        finally:
            connections.close_all()

    def scheduler_thread():
        connections["default"].settings_dict = copy.deepcopy(main_settings)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if close_due_elections():
                    return
                time.sleep(0.05)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=voter_thread, args=(v, i * 0.075))
               for i, v in enumerate(w["voters"])]  # spread over 3 seconds
    threads.append(threading.Thread(target=scheduler_thread))
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert accepted and refused, "the waves should straddle the closing time"
    assert all("not open" in r for r in refused)
    assert len(accepted) + len(refused) == 40
    ballots = _ballots(election)
    with acting_as(None):
        consumed = Entitlement.all_objects.filter(election=election, status="consumed").count()
        sealed = type(election).all_objects.get(pk=election.pk)
    assert len(ballots) == len(accepted) == consumed
    assert sealed.state == "closed"
    assert sealed.ballot_set_digest == encoding.ballot_set_digest(b.ciphertext for b in ballots)
