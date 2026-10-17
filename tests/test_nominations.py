from datetime import date

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, transaction

from audit.models import AuditEvent
from elections import lifecycle
from elections import services as election_services
from elections.eligibility import evaluate
from elections.models import Election
from members.models import Member
from nominations import services
from nominations.models import Candidate, Nomination, NominationDocument
from platform_core.db import acting_as, db_context
from tenancy.models import RoleAssignment
from tenancy.roles import Role
from tests.factories import (
    add_member,
    make_election,
    make_member,
    make_user,
    open_nominations,
    valid_rules,
)
from tests.helpers import login

pytestmark = pytest.mark.django_db
RO = {Role.RETURNING_OFFICER}


@pytest.fixture
def setup(two_orgs):
    """Organisation A with an RO, an election taking nominations and three members."""
    a, _ = two_orgs
    ro, ro_device = make_user("ro@example.org")
    add_member(a.org, ro, Role.RETURNING_OFFICER)
    election = open_nominations(make_election(a.org, a.admin), a.admin)
    people = {}
    for key, name in (("wanjiru", "Wanjiru Kamau"), ("otieno", "Otieno Ochieng"),
                      ("achieng", "Achieng Atieno")):
        user, device = make_user(f"{key}@example.org")
        add_member(a.org, user, Role.MEMBER)
        people[key] = (make_member(a.org, key.upper(), email=user.email, user=user,
                                   full_name=f"{name} (fictional)"), user, device)
    return a, ro, ro_device, election, people


def _ctx(org):
    return db_context(org_id=org.id)


def _to(election, target, actor, **kw):
    election_services.transition(election, target, actor=actor, roles=RO, **kw)
    election.refresh_from_db()


def _submit(election, submitter, nominee=None, **kw):
    kw.setdefault("proposer", None)
    kw.setdefault("seconder", None)
    return services.submit(election, submitter=submitter, nominee=nominee or submitter,
                           statement=kw.pop("statement", ""), actor=submitter.user, **kw)


# --- eligibility ---------------------------------------------------------------------

def test_eligibility_rules(two_orgs):
    a, _ = two_orgs
    rules = valid_rules()
    rules["eligibility"]["joined_before"] = "2024-01-01T00:00:00+03:00"
    ok = make_member(a.org, "E1", joined_on=date(2023, 12, 31))
    assert evaluate(ok, rules, "Africa/Nairobi").eligible
    on_cutoff_day = make_member(a.org, "E2", joined_on=date(2024, 1, 1))
    assert "joined on or after" in evaluate(on_cutoff_day, rules, "Africa/Nairobi").reasons[0]
    wrong = make_member(a.org, "E3", category="associate", subscription_status="unpaid")
    assert len(evaluate(wrong, rules, "Africa/Nairobi").reasons) == 2


# --- submission ---------------------------------------------------------------------

def test_self_nomination_is_complete_and_audited(setup):
    a, ro, _, election, people = setup
    wanjiru = people["wanjiru"][0]
    with transaction.atomic(), _ctx(a.org):
        n = _submit(election, wanjiru, statement="I will serve the club.")
        assert n.outstanding_confirmations == []
        assert AuditEvent.objects.filter(action="nomination.submitted", object_id=n.id).exists()


def test_nominating_someone_else_makes_you_proposer_and_needs_their_consent(setup):
    a, _, _, election, people = setup
    wanjiru, otieno = people["wanjiru"][0], people["otieno"][0]
    with transaction.atomic(), _ctx(a.org):
        n = _submit(election, wanjiru, nominee=otieno)
        assert n.proposer_id == wanjiru.id and n.outstanding_confirmations == ["nominee"]
        assert mail.outbox[-1].to == ["otieno@example.org"]
        services.confirm(n, otieno, actor=otieno.user)
        n.refresh_from_db()
        assert n.outstanding_confirmations == []


@pytest.mark.parametrize("rules_change,kwargs_builder,message", [
    ({"self_nomination": False}, lambda p: {}, "does not accept self-nominations"),
    ({"proposer_required": True}, lambda p: {}, "proposer is required"),
    ({"seconder_required": True}, lambda p: {}, "seconder is required"),
    ({}, lambda p: {"seconder": p["wanjiru"][0]}, "must be different people"),
])
def test_submission_rules(two_orgs, setup, rules_change, kwargs_builder, message):
    a, _, _, _, people = setup
    election = open_nominations(make_election(a.org, a.admin, title="Other"), a.admin,
                                rules=valid_rules(**rules_change))
    with transaction.atomic(), _ctx(a.org):
        with pytest.raises(services.NominationError, match=message):
            _submit(election, people["wanjiru"][0], **kwargs_builder(people))


def test_ineligible_people_and_duplicates_are_refused(setup):
    a, _, _, election, people = setup
    wanjiru = people["wanjiru"][0]
    unpaid = make_member(a.org, "U1", subscription_status="unpaid")
    with transaction.atomic(), _ctx(a.org):
        with pytest.raises(services.NominationError, match="nominee is not eligible"):
            _submit(election, wanjiru, nominee=unpaid)
        with pytest.raises(services.NominationError, match="seconder is not eligible"):
            _submit(election, wanjiru, seconder=unpaid)
        n = _submit(election, wanjiru)
        with pytest.raises(services.NominationError, match="already has a nomination"):
            _submit(election, people["otieno"][0], nominee=wanjiru)
        services.withdraw(n, wanjiru, actor=wanjiru.user)
        _submit(election, wanjiru)  # a withdrawn nomination may be resubmitted


def test_decline_and_withdraw_permissions(setup):
    a, _, _, election, people = setup
    wanjiru, otieno, achieng = (people[k][0] for k in ("wanjiru", "otieno", "achieng"))
    with transaction.atomic(), _ctx(a.org):
        n = _submit(election, wanjiru, nominee=otieno, seconder=achieng)
        with pytest.raises(services.NominationError, match="cannot withdraw"):
            services.withdraw(n, achieng, actor=achieng.user)  # the seconder cannot withdraw
        services.withdraw(n, achieng, actor=achieng.user, declined=True)  # but can decline
        n.refresh_from_db()
        assert n.status == "withdrawn" and n.status_reason == "Declined by the seconder"


# --- decisions and the candidate list -------------------------------------------------

def test_decisions_and_candidate_list(setup):
    a, ro, _, election, people = setup
    wanjiru, otieno, achieng = (people[k][0] for k in ("wanjiru", "otieno", "achieng"))
    with transaction.atomic(), _ctx(a.org):
        n_w = _submit(election, wanjiru)
        n_o = _submit(election, wanjiru, nominee=otieno)  # waiting for Otieno's consent
        n_a = _submit(election, achieng)
        with pytest.raises(services.NominationError, match="in review"):
            services.decide(n_w, approve=True, reason="", actor=ro)
        _to(election, lifecycle.REVIEW, ro)
        n_w.refresh_from_db()
        services.decide(n_w, approve=True, reason="", actor=ro)
        with pytest.raises(services.NominationError, match="waiting for confirmation"):
            services.decide(n_o, approve=True, reason="", actor=ro)
        with pytest.raises(services.NominationError, match="reason is required"):
            services.decide(n_o, approve=False, reason="", actor=ro)
        services.decide(n_o, approve=False, reason="Nominee did not consent in time", actor=ro)
        with pytest.raises(election_services.ElectionError, match="Decide every pending"):
            _to(election, lifecycle.READY, ro)
        services.decide(n_a, approve=True, reason="", actor=ro)
        assert RoleAssignment.objects.filter(role="candidate", election_id=election.id,
                                             valid_to__isnull=True).count() == 2
        # Reversing an approval removes the candidate and their candidate role.
        services.decide(n_a, approve=False, reason="Not a paid-up member", actor=ro)
        services.decide(n_a, approve=True, reason="Payment confirmed", actor=ro)
        _to(election, lifecycle.READY, ro)
        order = list(Candidate.objects.filter(election=election)
                     .values_list("display_name", "ballot_order"))
        assert order == [("Achieng Atieno (fictional)", 1), ("Wanjiru Kamau (fictional)", 2)]
        fixed = AuditEvent.objects.get(action="election.candidates_fixed", object_id=election.id)
        assert fixed.details["count"] == 2 and len(fixed.details["digest"]) == 64
        # The candidate list is frozen from here on, even for direct database writes.
        with pytest.raises(DatabaseError, match="only change while the election is in review"), \
                transaction.atomic():
            Candidate.objects.filter(election=election).update(display_name="Changed")
        with pytest.raises(DatabaseError, match="only be decided while"), transaction.atomic():
            Nomination.objects.filter(pk=n_o.pk).update(status="approved")


def test_random_order_is_a_permutation(setup):
    a, ro, _, _, people = setup
    rules = valid_rules()
    rules["candidate_order"] = "random"
    election = open_nominations(make_election(a.org, a.admin, title="Random"), a.admin,
                                rules=rules)
    with transaction.atomic(), _ctx(a.org):
        for key in people:
            _submit(election, people[key][0])
        _to(election, lifecycle.REVIEW, ro)
        for n in Nomination.objects.filter(election=election):
            services.decide(n, approve=True, reason="", actor=ro)
        _to(election, lifecycle.READY, ro)
        assert sorted(Candidate.objects.filter(election=election)
                      .values_list("ballot_order", flat=True)) == [1, 2, 3]


def test_no_candidates_blocks_submission_for_approval(setup):
    a, ro, _, election, _ = setup
    with transaction.atomic(), _ctx(a.org):
        _to(election, lifecycle.REVIEW, ro)
        with pytest.raises(election_services.ElectionError, match="no approved candidates"):
            _to(election, lifecycle.READY, ro)


# --- database guards ------------------------------------------------------------------

def test_database_guards_on_nominations(setup):
    a, ro, _, election, people = setup
    wanjiru, otieno = people["wanjiru"][0], people["otieno"][0]
    with transaction.atomic(), _ctx(a.org):
        n = _submit(election, wanjiru)
        with pytest.raises(DatabaseError, match="cannot change"), transaction.atomic():
            Nomination.objects.filter(pk=n.pk).update(nominee=otieno)
        with pytest.raises(DatabaseError, match="permission denied"), transaction.atomic(), \
                connection.cursor() as cursor:
            cursor.execute("DELETE FROM nomination WHERE id = %s", [n.pk])
        services.withdraw(n, wanjiru, actor=wanjiru.user)
        with pytest.raises(DatabaseError, match="withdrawn nomination is final"), \
                transaction.atomic():
            Nomination.objects.filter(pk=n.pk).update(status="submitted")
        _to(election, lifecycle.REVIEW, ro)
        with pytest.raises(DatabaseError, match="not open"), transaction.atomic():
            Nomination.objects.create(organisation=a.org, election=election, nominee=otieno,
                                      submitted_by=otieno)
    with acting_as(None), pytest.raises(DatabaseError, match="never deleted"), \
            transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM nomination WHERE id = %s", [n.pk])


def test_candidate_requires_approved_nomination(setup):
    a, ro, _, election, people = setup
    with transaction.atomic(), _ctx(a.org):
        n = _submit(election, people["wanjiru"][0])
        _to(election, lifecycle.REVIEW, ro)
        with pytest.raises(DatabaseError, match="approved nomination"), transaction.atomic():
            Candidate.objects.create(organisation=a.org, election=election, nomination=n,
                                     display_name="Sneaky", ballot_order=1)


# --- documents --------------------------------------------------------------------------

def _upload(name, data):
    return SimpleUploadedFile(name, data, content_type="application/pdf")


def test_documents_are_typed_by_content_and_limited(setup):
    a, _, _, election, people = setup
    wanjiru = people["wanjiru"][0]
    with transaction.atomic(), _ctx(a.org):
        n = _submit(election, wanjiru)
        with pytest.raises(services.NominationError, match="PDF, JPEG or PNG"):
            services.add_document(n, wanjiru, _upload("cv.pdf", b"<html><script>x</script>"),
                                  actor=wanjiru.user)
        with pytest.raises(services.NominationError, match="larger than 5 MB"):
            services.add_document(n, wanjiru, _upload("big.pdf", b"%PDF-" + b"0" * 5 * 2**20),
                                  actor=wanjiru.user)
        doc = services.add_document(n, wanjiru, _upload("../../etc/cv.png",
                                                        b"\x89PNG\r\n\x1a\n" + b"0" * 10),
                                    actor=wanjiru.user)
        assert doc.mime == "image/png" and doc.original_name == "cv.png"
        assert doc.storage_name.endswith(".png") and "cv" not in doc.storage_name
        for i in range(2):
            services.add_document(n, wanjiru, _upload(f"{i}.pdf", b"%PDF-1.4"),
                                  actor=wanjiru.user)
        with pytest.raises(services.NominationError, match="At most 3"):
            services.add_document(n, wanjiru, _upload("4.pdf", b"%PDF-1.4"),
                                  actor=wanjiru.user)
        otieno = people["otieno"][0]
        with pytest.raises(services.NominationError, match="Only the nominee"):
            services.add_document(n, otieno, _upload("x.pdf", b"%PDF-1.4"), actor=otieno.user)


def test_document_download_permissions_and_headers(client, setup):
    a, ro, ro_device, election, people = setup
    wanjiru, w_user, w_device = people["wanjiru"]
    login(client, w_user, w_device)
    r = client.post(f"/o/{a.org.slug}/elections/{election.id}/nominations/new/",
                    {"who": "self"})
    assert r.status_code == 302
    with acting_as(None):
        n = Nomination.all_objects.get(nominee=wanjiru)
    client.post(f"/o/{a.org.slug}/nominations/{n.id}/documents/",
                {"file": _upload("cv.pdf", b"%PDF-1.4 fictional")})
    with acting_as(None):
        doc = NominationDocument.all_objects.get(nomination=n)
    url = f"/o/{a.org.slug}/nomination-documents/{doc.id}/"
    r = client.get(url)
    assert r.status_code == 200
    assert r.headers["Content-Disposition"].startswith('attachment; filename="cv.pdf"')
    assert r.headers["Content-Type"] == "application/pdf"
    assert r.headers["Content-Security-Policy"] == "sandbox; default-src 'none'"
    assert b"".join(r.streaming_content) == b"%PDF-1.4 fictional"

    other = client.__class__()
    login(other, *people["otieno"][1:])
    assert other.get(url).status_code == 404  # another member: not found
    ro_client = client.__class__()
    login(ro_client, ro, ro_device)
    assert ro_client.get(url).status_code == 200


def test_officials_without_a_register_entry_cannot_nominate(client, setup):
    a, ro, ro_device, election, _ = setup
    outsider, device = make_user("external@example.org")
    add_member(a.org, outsider, Role.MEMBER)  # member role, but no register entry
    login(client, outsider, device)
    assert client.get(
        f"/o/{a.org.slug}/elections/{election.id}/nominations/new/").status_code == 403


def test_member_ui_confirm_flow(client, setup):
    a, _, _, election, people = setup
    login(client, *people["wanjiru"][1:])
    client.post(f"/o/{a.org.slug}/elections/{election.id}/nominations/new/",
                {"who": "other", "nominee_number": "OTIENO", "statement": "A fine candidate"})
    otieno_client = client.__class__()
    login(otieno_client, *people["otieno"][1:])
    page = otieno_client.get(f"/o/{a.org.slug}/elections/{election.id}/nominations/mine/")
    assert b"Waiting for confirmation from: nominee" in page.content
    with acting_as(None):
        n = Nomination.all_objects.get(nominee=people["otieno"][0])
    otieno_client.post(f"/o/{a.org.slug}/nominations/{n.id}/confirm/")
    with acting_as(None):
        n.refresh_from_db()
        assert n.nominee_consented_at is not None
        assert Election.all_objects.get(pk=election.pk).state == "nomination"


def test_member_actions_after_nominations_close_get_clear_errors(setup):
    a, ro, _, election, people = setup
    wanjiru, otieno = people["wanjiru"][0], people["otieno"][0]
    with transaction.atomic(), _ctx(a.org):
        n = _submit(election, wanjiru, nominee=otieno)
        _to(election, lifecycle.REVIEW, ro)
        n.refresh_from_db()
        with pytest.raises(services.NominationError, match="while nominations are open"):
            services.confirm(n, otieno, actor=otieno.user)
        with pytest.raises(services.NominationError, match="while nominations are open"):
            services.add_document(n, wanjiru, _upload("x.pdf", b"%PDF-1.4"), actor=wanjiru.user)
        services.withdraw(n, wanjiru, actor=wanjiru.user)  # still possible during review
    election2 = open_nominations(make_election(a.org, a.admin, title="Fixed"), a.admin)
    with transaction.atomic(), _ctx(a.org):
        n2 = _submit(election2, wanjiru)
        _to(election2, lifecycle.REVIEW, ro)
        n2.refresh_from_db()
        services.decide(n2, approve=True, reason="", actor=ro)
        _to(election2, lifecycle.READY, ro)
        n2.refresh_from_db()
        with pytest.raises(services.NominationError, match="candidate list is fixed"):
            services.withdraw(n2, wanjiru, actor=wanjiru.user)


def test_approval_rechecks_eligibility_at_decision_time(setup):
    a, ro, _, election, people = setup
    wanjiru = people["wanjiru"][0]
    with transaction.atomic(), _ctx(a.org):
        n = _submit(election, wanjiru)
        _to(election, lifecycle.REVIEW, ro)
        Member.objects.filter(pk=wanjiru.pk).update(subscription_status="unpaid")
        n.refresh_from_db()
        with pytest.raises(services.NominationError, match="nominee is not eligible"):
            services.decide(n, approve=True, reason="", actor=ro)
