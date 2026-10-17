"""Counting, certification, publication, results and the receipt check."""

import json

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.stepup import step_up_required
from elections import lifecycle, rules
from elections.models import Election
from keys import services as key_services
from tenancy.permissions import requires
from tenancy.roles import Perm

from . import services
from .forms import ContributeForm, StartCountForm, TieForm
from .models import KeyContribution

COUNTING_STATES = (lifecycle.CLOSED, *services.RECOUNT_STATES)


def _election(election_id) -> Election:
    return get_object_or_404(
        Election.objects.select_related("current_rule_version", "organisation"), pk=election_id)


def _to_count(org_slug, election):
    return redirect("tally:count", org_slug=org_slug, election_id=election.id)


def _lines(tally):
    outcomes = services.final_outcomes(tally)
    return [{"line": line, "final": outcomes[line.candidate_id]}
            for line in tally.lines.select_related("candidate")]


@require_GET
@requires(Perm.ELECTIONS_VIEW)
def count_page(request, org_slug, election_id):
    perms = request.permissions
    if not perms & {Perm.TALLY_VIEW, Perm.TALLY_RUN, Perm.KEYS_HOLD, Perm.RESULTS_PUBLISH}:
        raise PermissionDenied
    election = _election(election_id)
    waiting = services.open_request(election)
    staged = (list(KeyContribution.objects.filter(count_request=waiting,
                                                  expires_at__gte=timezone.now())
                   .select_related("contributed_by")) if waiting else [])
    tally = services.latest_tally(election)
    can_view = Perm.TALLY_VIEW in perms
    certification = services.active_certification(election)
    return render(request, "tally/count.html", {
        "election": election,
        "waiting": waiting,
        "staged": staged,
        "my_share": key_services.share_index_for(election, {"user": request.user}),
        "already_contributed": any(c.contributed_by_id == request.user.id for c in staged),
        "key_shares": election.key_shares.all(),
        "can_run": Perm.TALLY_RUN in perms,
        "can_view": can_view,
        "can_hold": Perm.KEYS_HOLD in perms,
        "can_start": (Perm.TALLY_RUN in perms and waiting is None
                      and election.state in COUNTING_STATES),
        "recount": election.state != lifecycle.CLOSED,
        "tally": tally if can_view else None,
        "lines": _lines(tally) if tally and can_view else [],
        "resolution": services.tie_resolution(tally) if tally and tally.has_ties else None,
        "blockers": services.certification_blockers(election, tally) if tally else [],
        "tallies": election.tallies.select_related("run_by") if can_view else [],
        "certification": certification,
        "can_publish": (Perm.RESULTS_PUBLISH in perms and election.state == lifecycle.CERTIFIED
                        and certification is not None and certification.published_at is None),
        "ttl_minutes": int(services.CONTRIBUTION_TTL.total_seconds() // 60),
    })


@require_http_methods(["GET", "POST"])
@requires(Perm.TALLY_RUN)
@step_up_required
def start(request, org_slug, election_id):
    election = _election(election_id)
    recount = election.state != lifecycle.CLOSED
    form = StartCountForm(request.POST if request.method == "POST" else None, recount=recount)
    if request.method == "POST" and form.is_valid():
        try:
            services.start_count(election, actor=request.user,
                                 reason=form.cleaned_data["reason"])
        except services.TallyError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Counting started. Two keyholders must now provide their "
                                      "shares on this page.")
            return _to_count(org_slug, election)
    return render(request, "tally/start.html", {"election": election, "form": form,
                                                "recount": recount})


@require_http_methods(["GET", "POST"])
@requires(Perm.TALLY_RUN)
@step_up_required
def cancel(request, org_slug, election_id):
    election = _election(election_id)
    waiting = services.open_request(election)
    if waiting is None:
        messages.error(request, "No count is waiting.")
        return _to_count(org_slug, election)
    if request.method == "POST":
        services.cancel_count(waiting, actor=request.user)
        messages.success(request, "Counting cancelled. Any share already provided was deleted.")
        return _to_count(org_slug, election)
    return render(request, "tally/confirm.html", {
        "election": election, "title": "Cancel this count?",
        "text": "Any share already provided is deleted; keyholders would have to provide "
                "them again.", "button": "Cancel the count"})


@sensitive_post_parameters("passphrase", "backup_code")
@require_POST
@requires(Perm.KEYS_HOLD)
def contribute(request, org_slug, election_id):
    election = _election(election_id)
    waiting = services.open_request(election)
    form = ContributeForm(request.POST)
    if waiting is None:
        messages.error(request, "No count is waiting for keyholders.")
        return _to_count(org_slug, election)
    if form.is_valid():
        try:
            result = services.contribute(
                waiting, actor=request.user, passphrase=form.cleaned_data["passphrase"],
                backup_code=form.cleaned_data["backup_code"].strip() or None)
        except services.TallyError as exc:
            messages.error(request, str(exc))
        else:
            if result.status == "staged":
                messages.success(request, "Your share was provided. A second keyholder must now "
                                          "provide theirs within "
                                          f"{int(services.CONTRIBUTION_TTL.total_seconds() // 60)}"
                                          " minutes.")
            else:
                messages.success(request, f"Counted (version {result.tally.version}).")
    else:
        for error in form.non_field_errors():
            messages.error(request, error)
    return _to_count(org_slug, election)


@require_http_methods(["GET", "POST"])
@requires(Perm.TALLY_RUN)
@step_up_required
def tie(request, org_slug, election_id):
    election = _election(election_id)
    tally = services.latest_tally(election)
    if (election.state != lifecycle.COUNTED or tally is None or not tally.has_ties
            or services.tie_resolution(tally) is not None):
        messages.error(request, "There is no unresolved tie.")
        return _to_count(org_slug, election)
    lines = list(tally.lines.select_related("candidate"))
    tied = [line for line in lines if line.outcome == "tied_pending"]
    seats = election.seats - sum(1 for line in lines if line.outcome == "elected")
    form = TieForm(request.POST or None, seats=seats,
                   choices=[(str(t.candidate_id), t.candidate.display_name) for t in tied])
    if request.method == "POST" and form.is_valid():
        try:
            services.resolve_tie(election, actor=request.user,
                                 elected_ids=form.cleaned_data["elected"],
                                 evidence=form.cleaned_data["evidence"])
        except services.TallyError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Tie resolution recorded.")
            return _to_count(org_slug, election)
    procedure = election.current_rule_version.rules["tie_procedure"]
    return render(request, "tally/tie.html", {
        "election": election, "form": form, "tied": tied, "seats": seats,
        "procedure": rules.TIE_PROCEDURES[procedure["type"]],
        "description": procedure["description"]})


@require_http_methods(["GET", "POST"])
@requires(Perm.TALLY_RUN)
@step_up_required
def certify(request, org_slug, election_id):
    election = _election(election_id)
    tally = services.latest_tally(election)
    error = None
    if request.method == "POST":
        try:
            certification = services.certify(election, actor=request.user)
        except services.TallyError as exc:
            error = str(exc)
        else:
            messages.success(request, "Result certified. Fingerprint: "
                                      f"{certification.content_sha256[:16]}…")
            return _to_count(org_slug, election)
    if election.state != lifecycle.AWAITING or tally is None:
        messages.error(request, "Only an election awaiting certification can be certified.")
        return _to_count(org_slug, election)
    preview = services.result_document(election, tally, certified_by=request.user,
                                       certified_at=timezone.now())
    return render(request, "tally/certify.html", {
        "election": election, "doc": preview, "error": error,
        "blockers": services.certification_blockers(election, tally)})


@require_http_methods(["GET", "POST"])
@requires(Perm.RESULTS_PUBLISH)
@step_up_required
def publish(request, org_slug, election_id):
    election = _election(election_id)
    if request.method == "POST":
        try:
            services.publish(election, actor=request.user)
        except services.TallyError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Results published to members.")
        return _to_count(org_slug, election)
    certification = services.active_certification(election)
    if certification is None:
        messages.error(request, "There is no certified result to publish.")
        return _to_count(org_slug, election)
    return render(request, "tally/confirm.html", {
        "election": election, "title": "Publish the certified result to members?",
        "text": f"Every member of {request.org.legal_name} will be able to see the certified "
                f"result (fingerprint {certification.content_sha256[:16]}…). This cannot be "
                "undone.", "button": "Publish"})


@require_GET
@requires(Perm.ORG_VIEW)
def results(request, org_slug, election_id):
    election = _election(election_id)
    certification = services.published_result(election)
    withdrawn = None
    if certification is None:
        withdrawn = (election.certifications.filter(published_at__isnull=False,
                                                    superseded_at__isnull=False)
                     .order_by("-superseded_at").first())
    return render(request, "tally/results.html", {
        "election": election, "certification": certification,
        "doc": json.loads(certification.document) if certification else None,
        "withdrawn": withdrawn})


@require_GET
@requires(Perm.ORG_VIEW)
def result_document(request, org_slug, election_id):
    """The certified document, byte for byte; its SHA-256 is the published fingerprint."""
    certification = services.published_result(_election(election_id))
    if certification is None:
        return redirect("tally:results", org_slug=org_slug, election_id=election_id)
    response = HttpResponse(certification.document.encode(),
                            content_type="application/json; charset=utf-8")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="result-{election_id}.json"')
    return response


@require_GET
@requires(Perm.ORG_VIEW)
def receipts(request, org_slug, election_id):
    election = _election(election_id)
    commitments = services.receipt_commitments(election) if election.ballot_set_digest else []
    return render(request, "tally/receipts.html", {
        "election": election, "sealed": bool(election.ballot_set_digest),
        "commitments": commitments, "digest": services.receipts_digest(commitments)})

