import hashlib
import re
from datetime import timedelta

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from audit.models import AuditEvent
from elections import lifecycle, rules, services
from elections.models import Election, ElectionRuleVersion, LifecycleTransition
from nominations import services as nomination_services
from nominations.models import Nomination
from platform_core.db import acting_as, db_context
from tenancy.models import RoleAssignment
from tenancy.roles import Role
from tests.factories import add_member, make_election, make_member, make_user, totp_now
from tests.helpers import login

pytestmark = pytest.mark.django_db

CATEGORIES = ("full", "associate", "life", "honorary")


def valid_rules(seats=3, **overrides):
    r = rules.default_rules(seats, ("full", "life"))
    r["tie_procedure"]["description"] = "Lots are drawn by the Returning Officer in public."
    r.update(overrides)
    return r


@pytest.fixture
def ro(two_orgs):
    a, _ = two_orgs
    user, device = make_user("ro@example.org")
    add_member(a.org, user, Role.RETURNING_OFFICER)
    return user, device


def _in_org(org):
    """Run as app_rw in the organisation's context (like a request)."""
    return db_context(org_id=org.id)


def _set_reason(reason):
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.transition_reason', %s, true)", [reason])


def _prepared(two_orgs, ro_user, *, state=lifecycle.SCHEDULED, rules_author=None):
    """An election taken through the service up to ``state`` (scheduled by default)."""
    a, _ = two_orgs
    election = make_election(a.org, a.admin)
    nominee = make_member(a.org, f"C-{election.id.hex[:6]}")
    with transaction.atomic(), _in_org(a.org):
        services.save_rules(election, valid_rules(), actor=rules_author or a.admin)
        for target in (lifecycle.NOMINATION, lifecycle.REVIEW, lifecycle.READY,
                       lifecycle.SCHEDULED):
            if target == lifecycle.READY:  # an approved candidate is needed from here on
                nomination = Nomination.objects.get(election=election)
                nomination_services.decide(nomination, approve=True, reason="", actor=ro_user)
            services.transition(election, target, actor=ro_user, roles={Role.RETURNING_OFFICER})
            if target == lifecycle.NOMINATION:
                election.refresh_from_db()
                nomination_services.submit(election, submitter=nominee, nominee=nominee,
                                           proposer=None, seconder=None, statement="",
                                           actor=a.admin)
            if target == state:
                break
    with acting_as(None):
        return Election.all_objects.get(pk=election.pk)


# --- rules ------------------------------------------------------------------------

def test_default_rules_need_only_a_tie_description():
    r = rules.default_rules(3, CATEGORIES)
    assert any("tie-break" in p for p in rules.validate_rules(r, 3, CATEGORIES))
    r["tie_procedure"]["description"] = "Lots drawn by the Returning Officer."
    assert rules.validate_rules(r, 3, CATEGORIES) == []


@pytest.mark.parametrize("change,message", [
    ({"max_selections": 4}, "at most between 1 and 3"),
    ({"min_selections": 2}, "blank ballot needs a minimum of 0"),
    ({"partial_ballots_allowed": False}, "minimum must equal the maximum"),
    ({"quorum": {"type": "percent_of_electorate", "basis": "accepted_ballots",
                 "threshold": 150}}, "at most 100"),
    ({"eligibility": {"member_status": ["active"], "categories": ["platinum"],
                      "subscription_status": ["paid"], "joined_before": None}},
     "Unknown membership category"),
    ({"eligibility": {"member_status": ["active"], "categories": ["full"],
                      "subscription_status": ["paid"], "joined_before": "2026-01-01T00:00"}},
     "needs a time zone"),
])
def test_rule_validation(change, message):
    r = valid_rules(**change)
    assert any(message in p for p in rules.validate_rules(r, 3, CATEGORIES))


def test_rule_versions_and_database_fingerprint(two_orgs):
    a, _ = two_orgs
    election = make_election(a.org, a.admin)
    with transaction.atomic(), _in_org(a.org):
        v1 = services.save_rules(election, valid_rules(), actor=a.admin)
        v2 = services.save_rules(election, valid_rules(max_selections=2), actor=a.admin)
        assert (v1.version, v2.version) == (1, 2)
        with connection.cursor() as cursor:
            cursor.execute("SELECT rules::text FROM election_rule_version WHERE id = %s",
                           [v2.id])
            text = cursor.fetchone()[0]
        assert v2.rules_sha256 == hashlib.sha256(text.encode()).hexdigest()
        assert Election.objects.get(pk=election.pk).current_rule_version_id == v2.id


# --- lifecycle through the service ------------------------------------------------

def test_python_and_database_transition_tables_match():
    with connection.cursor() as cursor:
        cursor.execute("SELECT from_state, to_state, reason_required FROM allowed_transition")
        db = {(a, b): r for a, b, r in cursor.fetchall()}
    assert db == {k: t.reason_required for k, t in lifecycle.TRANSITIONS.items()}


def test_happy_path_to_scheduled_records_history_and_approval(two_orgs, ro):
    ro_user, _ = ro
    election = _prepared(two_orgs, ro_user)
    assert election.state == lifecycle.SCHEDULED
    with acting_as(None):
        history = list(LifecycleTransition.all_objects.filter(election=election)
                       .values_list("from_state", "to_state", "actor_id"))
        version = ElectionRuleVersion.all_objects.get(pk=election.current_rule_version_id)
        approvals = AuditEvent.objects.filter(action="election.rules_approved",
                                              object_id=election.id).count()
    assert [h[:2] for h in history] == [("draft", "nomination"), ("nomination", "review"),
                                        ("review", "ready_for_approval"),
                                        ("ready_for_approval", "scheduled")]
    assert all(h[2] == ro_user.id for h in history)
    assert version.approved_by_id == ro_user.id and version.approved_at is not None
    assert approvals == 1


def test_rules_author_cannot_approve_them(two_orgs, ro):
    a, _ = two_orgs
    ro_user, _ = ro
    election = _prepared(two_orgs, ro_user, state=lifecycle.READY, rules_author=ro_user)
    with transaction.atomic(), _in_org(a.org):
        with pytest.raises(services.ElectionError, match="someone other than"):
            services.transition(election, lifecycle.SCHEDULED, actor=ro_user,
                                roles={Role.RETURNING_OFFICER})


def test_role_checks(two_orgs, ro):
    a, _ = two_orgs
    election = make_election(a.org, a.admin)
    with transaction.atomic(), _in_org(a.org):
        services.save_rules(election, valid_rules(), actor=a.admin)
        services.transition(election, lifecycle.NOMINATION, actor=a.admin,
                            roles={Role.ORG_ADMIN})  # admins may open nominations
        with pytest.raises(PermissionDenied):
            services.transition(election, lifecycle.REVIEW, actor=a.admin,
                                roles={Role.ORG_ADMIN})  # only the RO closes them


def test_reason_required_and_unavailable_transitions(two_orgs, ro):
    ro_user, _ = ro
    election = _prepared(two_orgs, ro_user, state=lifecycle.READY)
    a, _ = two_orgs
    with transaction.atomic(), _in_org(a.org):
        with pytest.raises(services.ElectionError, match="reason is required"):
            services.transition(election, lifecycle.DRAFT, actor=ro_user,
                                roles={Role.RETURNING_OFFICER})
        services.transition(election, lifecycle.DRAFT, actor=ro_user,
                            roles={Role.RETURNING_OFFICER}, reason="Seats changed by board")
        assert LifecycleTransition.objects.filter(
            election=election, reason="Seats changed by board").exists()
    scheduled = _prepared(two_orgs, ro_user)
    with transaction.atomic(), _in_org(a.org):
        with pytest.raises(services.ElectionError, match="not available yet"):
            services.transition(scheduled, lifecycle.OPEN, actor=ro_user,
                                roles={Role.RETURNING_OFFICER})


def test_opening_nominations_requires_rules(two_orgs, ro):
    a, _ = two_orgs
    ro_user, _ = ro
    election = make_election(a.org, a.admin)
    with transaction.atomic(), _in_org(a.org):
        with pytest.raises(services.ElectionError, match="rules before opening nominations"):
            services.transition(election, lifecycle.NOMINATION, actor=ro_user,
                                roles={Role.RETURNING_OFFICER})


def test_voting_period_minimum(two_orgs):
    a, _ = two_orgs
    election = make_election(a.org, a.admin)
    before = services.snapshot(election)
    election.closes_at = election.opens_at + timedelta(minutes=30)
    with transaction.atomic(), _in_org(a.org):
        with pytest.raises(services.ElectionError, match="at least one hour"):
            services.update_election(election, actor=a.admin, before=before)


# --- the database enforces the lifecycle on its own (acceptance test 12) ----------

def test_database_rejects_illegal_transitions_and_missing_reasons(two_orgs):
    a, _ = two_orgs
    election = make_election(a.org, a.admin)
    with transaction.atomic(), _in_org(a.org):
        with pytest.raises(DatabaseError, match="not allowed"), transaction.atomic():
            Election.objects.filter(pk=election.pk).update(state="open")
        with pytest.raises(DatabaseError, match="requires a reason"), transaction.atomic():
            Election.objects.filter(pk=election.pk).update(state="cancelled")


def test_new_elections_must_start_in_draft(two_orgs):
    a, _ = two_orgs
    with acting_as(None):  # even the owner
        with pytest.raises(DatabaseError, match="starts in draft"), transaction.atomic():
            Election.all_objects.create(organisation=a.org, title="x", seats=1,
                                        created_by=a.admin, state="open")


def test_configuration_locked_after_submission_and_closed_is_final(two_orgs, ro):
    a, _ = two_orgs
    ro_user, _ = ro
    election = _prepared(two_orgs, ro_user)  # scheduled
    with transaction.atomic(), _in_org(a.org):
        for change in ({"seats": 5}, {"opens_at": timezone.now() + timedelta(days=60)},
                       {"closes_at": election.closes_at + timedelta(days=1)},
                       {"title": "Renamed"}):
            with pytest.raises(DatabaseError, match="locked|closing time"), transaction.atomic():
                Election.objects.filter(pk=election.pk).update(**change)
        # A new rule version cannot be added and the approved one cannot change.
        with pytest.raises(DatabaseError, match="being prepared"), transaction.atomic():
            ElectionRuleVersion.objects.create(organisation=a.org, election=election, version=9,
                                               rules=valid_rules(), created_by=a.admin)
        with pytest.raises(DatabaseError), transaction.atomic():
            ElectionRuleVersion.objects.filter(pk=election.current_rule_version_id).update(
                approved_at=timezone.now())
        # Raw SQL (Django's PROTECT check would fire first). app_rw has no DELETE
        # grant at all; the trigger stops even the owner (checked further down).
        with pytest.raises(DatabaseError, match="permission denied"), transaction.atomic(), \
                connection.cursor() as cursor:
            cursor.execute("DELETE FROM election_rule_version WHERE id = %s",
                           [election.current_rule_version_id])

        # Walk the system transitions directly to test suspension and closing.
        Election.objects.filter(pk=election.pk).update(state="open")
        _set_reason("Power outage at polling hub")
        Election.objects.filter(pk=election.pk).update(state="suspended")
        Election.objects.filter(pk=election.pk).update(
            closes_at=election.closes_at + timedelta(hours=6))  # allowed while suspended
        _set_reason("Power restored")
        Election.objects.filter(pk=election.pk).update(state="open")
        _set_reason("")
        Election.objects.filter(pk=election.pk).update(state="closed")
        for change in ({"state": "open"}, {"state": "draft"}, {"seats": 1},
                       {"closes_at": timezone.now() + timedelta(days=9)}):
            with pytest.raises(DatabaseError), transaction.atomic():
                Election.objects.filter(pk=election.pk).update(**change)
        with pytest.raises(DatabaseError, match="permission denied"), transaction.atomic(), \
                connection.cursor() as cursor:
            cursor.execute("DELETE FROM election WHERE id = %s", [election.pk])
        reasons = list(LifecycleTransition.objects.filter(election=election)
                       .exclude(reason="").values_list("reason", flat=True))
    assert reasons == ["Power outage at polling hub", "Power restored"]
    with acting_as(None):  # the owner bypasses grants but not the triggers
        for sql in ("DELETE FROM election WHERE id = %s",
                    "DELETE FROM election_rule_version WHERE election_id = %s"):
            with pytest.raises(DatabaseError, match="never deleted"), transaction.atomic(), \
                    connection.cursor() as cursor:
                cursor.execute(sql, [election.pk])


def test_cancelled_is_terminal(two_orgs):
    a, _ = two_orgs
    election = make_election(a.org, a.admin)
    with transaction.atomic(), _in_org(a.org):
        services.transition(election, lifecycle.CANCELLED, actor=a.admin,
                            roles={Role.ORG_ADMIN}, reason="Board decision")
        with pytest.raises(DatabaseError, match="cancelled"), transaction.atomic():
            Election.objects.filter(pk=election.pk).update(title="Revived")


def test_history_cannot_be_written_or_altered_by_the_app(two_orgs):
    a, _ = two_orgs
    election = make_election(a.org, a.admin)
    with transaction.atomic(), _in_org(a.org):
        services.save_rules(election, valid_rules(), actor=a.admin)
        services.transition(election, lifecycle.NOMINATION, actor=a.admin,
                            roles={Role.ORG_ADMIN})
        with pytest.raises(DatabaseError), transaction.atomic():
            LifecycleTransition.objects.create(organisation=a.org, election=election,
                                               from_state="x", to_state="y")
        with pytest.raises(DatabaseError), transaction.atomic():
            LifecycleTransition.objects.filter(election=election).update(reason="edited")
    with acting_as(None):  # even the owner cannot rewrite it
        with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
            LifecycleTransition.all_objects.filter(election=election).delete()


# --- through the web UI ------------------------------------------------------------

def test_ui_flow_create_rules_and_schedule(client, two_orgs, ro):
    a, _ = two_orgs
    ro_user, ro_device = ro
    login(client, a.admin, a.admin_device)
    opens = (timezone.localtime(timezone.now() + timedelta(days=14))).strftime("%Y-%m-%dT%H:%M")
    closes = (timezone.localtime(timezone.now() + timedelta(days=16))).strftime("%Y-%m-%dT%H:%M")
    r = client.post(f"/o/{a.org.slug}/elections/new/", {
        "title": "Directors 2026 (fictional)", "seats": 3, "opens_at": opens, "closes_at": closes})
    assert r.status_code == 302
    election_url = r.url
    rules_form = {
        "max_selections": 3, "min_selections": 0, "partial_ballots_allowed": "on",
        "blank_ballot_allowed": "on", "categories": ["full", "life"],
        "subscription_status": ["paid", "exempt"], "self_nomination": "on",
        "tie_type": "lot_drawn_by_returning_officer",
        "tie_description": "Lots drawn publicly by the Returning Officer.",
        "quorum_type": "percent_of_electorate", "quorum_basis": "accepted_ballots",
        "quorum_threshold": "25", "candidate_order": "alphabetical",
    }
    assert client.post(f"{election_url}rules/", {**rules_form, "max_selections": 4}
                       ).status_code == 200  # more than the seats: rejected
    assert client.post(f"{election_url}rules/", rules_form).status_code == 302
    r = client.post(f"{election_url}transition/nomination/")
    assert r.status_code == 302, r.content.decode()[-1500:]
    page = client.get(election_url).content.decode()
    assert "Nominations open" in page and "25% of the electorate" in page

    # A member nominates themself through the UI.
    member_user, member_device = make_user("candidate@example.org")
    add_member(a.org, member_user, Role.MEMBER)
    make_member(a.org, "C-1", email=member_user.email, user=member_user,
                full_name="Amani Candidate (fictional)")
    member_client = client.__class__()
    login(member_client, member_user, member_device)
    assert "Taking nominations" in member_client.get(f"/o/{a.org.slug}/").content.decode()
    r = member_client.post(f"{election_url}nominations/new/", {"who": "self",
                                                              "statement": "I will serve."})
    assert r.status_code == 302

    ro_client = client.__class__()
    login(ro_client, ro_user, ro_device)
    ro_client.post(f"{election_url}transition/review/")
    with acting_as(None):
        nomination_id = Nomination.all_objects.get(nominee__member_number="C-1").id
    r = ro_client.post(f"/o/{a.org.slug}/nominations/{nomination_id}/decide/",
                       {"decision": "approve"})
    assert r.status_code == 302
    ro_client.post(f"{election_url}transition/ready_for_approval/")
    session = ro_client.session
    session["mfa_at"] = 0
    session.save()
    r = ro_client.get(f"{election_url}transition/scheduled/")
    assert r.status_code == 302 and "/accounts/step-up/" in r.url
    ro_device.refresh_from_db()
    ro_client.post(f"/accounts/step-up/?next={election_url}transition/scheduled/",
                   {"token": totp_now(ro_device, offset_steps=1)})
    page = ro_client.get(f"{election_url}transition/scheduled/").content.decode()
    assert "rules version 1" in page
    assert ro_client.post(f"{election_url}transition/scheduled/").status_code == 302
    with acting_as(None):
        assert Election.all_objects.get(title="Directors 2026 (fictional)").state == "scheduled"
    assert client.get(f"{election_url}edit/").status_code == 302  # locked: redirected
    assert b"Edit settings" not in client.get(election_url).content


@pytest.mark.parametrize("role,view,edit", [
    (Role.AUDITOR, 200, 403), (Role.MEMBER, 403, 403), (Role.RETURNING_OFFICER, 200, 200)])
def test_election_permissions(client, two_orgs, role, view, edit):
    a, _ = two_orgs
    user, device = make_user(f"{role}@example.org")
    add_member(a.org, user, role)
    login(client, user, device)
    assert client.get(f"/o/{a.org.slug}/elections/").status_code == view
    assert client.get(f"/o/{a.org.slug}/elections/new/").status_code == edit


# --- officials who are not members ---------------------------------------------------

def test_invite_official_end_to_end(client, two_orgs):
    a, _ = two_orgs
    login(client, a.admin, a.admin_device)
    r = client.post(f"/o/{a.org.slug}/roles/invite/", {
        "email": "Independent.RO@Example.org", "full_name": "Jane Independent (fictional)",
        "role": "returning_officer"})
    assert r.status_code == 302
    (message,) = mail.outbox
    assert "appointed Returning Officer" in message.subject
    token = re.search(r"https://vote\.example/invite/([^/\s]+)/", message.body).group(1)
    assert b"account not set up yet" in client.get(f"/o/{a.org.slug}/roles/").content

    invitee = client.__class__()
    invitee.get(f"/invite/{token}/")
    strong = "a perfectly long passphrase"
    r = invitee.post("/invite/accept/", {"password1": strong, "password2": strong})
    assert r.url == "/accounts/mfa/setup/"
    from django_otp.plugins.otp_totp.models import TOTPDevice
    invitee.get("/accounts/mfa/setup/")
    with acting_as(None):
        device = TOTPDevice.objects.get(user__email="independent.ro@example.org")
    invitee.post("/accounts/mfa/setup/", {"token": totp_now(device)})
    invitee.post("/accounts/mfa/recovery-codes/")
    assert invitee.get(f"/o/{a.org.slug}/elections/").status_code == 200
    assert invitee.get(f"/o/{a.org.slug}/members/").status_code == 200  # RO may view register
    with acting_as(None):
        assert AuditEvent.objects.filter(action="official.invitation_accepted").count() == 1


def test_only_admins_invite_officials_and_not_twice(client, two_orgs, ro):
    a, _ = two_orgs
    ro_user, ro_device = ro
    ro_client = client.__class__()
    login(ro_client, ro_user, ro_device)
    assert ro_client.get(f"/o/{a.org.slug}/roles/invite/").status_code == 403
    login(client, a.admin, a.admin_device)
    r = client.post(f"/o/{a.org.slug}/roles/invite/", {
        "email": "ro@example.org", "full_name": "x", "role": "returning_officer"})
    assert b"already has that role" in r.content
    with acting_as(None):
        assert RoleAssignment.all_objects.filter(membership__user=ro_user,
                                                 role="returning_officer").count() == 1


def test_rules_form_round_trips_membership_cutoff_in_nairobi_time(client, two_orgs):
    a, _ = two_orgs
    election = make_election(a.org, a.admin)
    login(client, a.admin, a.admin_device)
    url = f"/o/{a.org.slug}/elections/{election.id}/rules/"
    form = client.get(url).context["form"]
    data = {k: v for k, v in form.initial.items() if v is not None and v is not False}
    data.update({"tie_description": "Lots drawn publicly by the Returning Officer.",
                 "joined_before": "2026-09-30T18:00", "quorum_threshold": "0"})
    r = client.post(url, data)
    assert r.status_code == 302, r.context["form"].errors
    with acting_as(None):
        saved = Election.all_objects.get(pk=election.pk).current_rule_version.rules
    assert saved["eligibility"]["joined_before"] == "2026-09-30T18:00:00+03:00"
    page = client.get(url).content.decode()
    assert 'value="2026-09-30T18:00"' in page
