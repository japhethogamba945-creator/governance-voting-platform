from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.stepup import step_up_required
from elections import lifecycle
from elections.eligibility import evaluate
from elections.models import Election
from tenancy.permissions import requires
from tenancy.roles import Perm

from . import services
from .forms import DecisionForm, DocumentForm, NominateForm, StatementForm
from .models import Candidate, Nomination, NominationDocument


def _election(election_id) -> Election:
    return get_object_or_404(Election.objects.select_related("current_rule_version",
                                                             "organisation"), pk=election_id)


def _nomination(nomination_id) -> Nomination:
    return get_object_or_404(
        Nomination.objects.select_related("election__current_rule_version",
                                          "election__organisation", "nominee", "proposer",
                                          "seconder", "submitted_by"), pk=nomination_id)


def _my_member(request):
    member = services.member_for(request.user)
    if member is None:
        raise PermissionDenied("Only active members on the register can take part in "
                               "nominations.")
    return member


def _back(nomination):
    return redirect("nominations:mine", org_slug=nomination.organisation.slug,
                    election_id=nomination.election_id)


# --- members ------------------------------------------------------------------------

@require_GET
@requires(Perm.NOMINATIONS_SUBMIT)
def my_nominations(request, org_slug, election_id):
    election = _election(election_id)
    member = _my_member(request)
    eligibility = (evaluate(member, election.current_rule_version.rules, request.org.timezone)
                   if election.current_rule_version else None)
    items = [(n, services.roles_of(member, n)) for n in
             services.nominations_for_member(election, member)]
    return render(request, "nominations/mine.html", {
        "election": election, "member": member, "items": items,
        "eligibility": eligibility,
        "open": election.state == lifecycle.NOMINATION,
        "rules": election.current_rule_version.rules["nomination"]
        if election.current_rule_version else None,
        "document_form": DocumentForm(), "statement_form": StatementForm(),
    })


@require_http_methods(["GET", "POST"])
@requires(Perm.NOMINATIONS_SUBMIT)
def nominate(request, org_slug, election_id):
    election = _election(election_id)
    member = _my_member(request)
    if election.state != lifecycle.NOMINATION:
        messages.error(request, "Nominations are not open for this election.")
        return redirect("nominations:mine", org_slug=org_slug, election_id=election.id)
    form = NominateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        nominee = member if form.cleaned_data["who"] == "self" else form.cleaned_data["nominee"]
        try:
            services.submit(election, submitter=member, nominee=nominee,
                            proposer=form.cleaned_data["proposer"],
                            seconder=form.cleaned_data["seconder"],
                            statement=form.cleaned_data["statement"], actor=request.user)
        except services.NominationError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Nomination submitted.")
            return redirect("nominations:mine", org_slug=org_slug, election_id=election.id)
    return render(request, "nominations/nominate.html", {
        "form": form, "election": election,
        "rules": election.current_rule_version.rules["nomination"]})


def _member_action(request, nomination_id, action):
    nomination = _nomination(nomination_id)
    member = _my_member(request)
    try:
        action(nomination, member)
    except services.NominationError as exc:
        messages.error(request, str(exc))
    return _back(nomination)


@require_POST
@requires(Perm.NOMINATIONS_SUBMIT)
def confirm(request, org_slug, nomination_id):
    def act(n, m):
        services.confirm(n, m, actor=request.user)
        messages.success(request, "Thank you. Your confirmation is recorded.")
    return _member_action(request, nomination_id, act)


@require_POST
@requires(Perm.NOMINATIONS_SUBMIT)
def decline(request, org_slug, nomination_id):
    def act(n, m):
        services.withdraw(n, m, actor=request.user, declined=True)
        messages.success(request, "You declined the nomination. It has been withdrawn.")
    return _member_action(request, nomination_id, act)


@require_POST
@requires(Perm.NOMINATIONS_SUBMIT)
def withdraw(request, org_slug, nomination_id):
    def act(n, m):
        services.withdraw(n, m, actor=request.user)
        messages.success(request, "The nomination has been withdrawn.")
    return _member_action(request, nomination_id, act)


@require_POST
@requires(Perm.NOMINATIONS_SUBMIT)
def edit_statement(request, org_slug, nomination_id):
    form = StatementForm(request.POST)

    def act(n, m):
        if form.is_valid():
            services.update_statement(n, m, form.cleaned_data["statement"], actor=request.user)
            messages.success(request, "Statement updated.")
    return _member_action(request, nomination_id, act)


@require_POST
@requires(Perm.NOMINATIONS_SUBMIT)
def add_document(request, org_slug, nomination_id):
    form = DocumentForm(request.POST, request.FILES)

    def act(n, m):
        if not form.is_valid():
            raise services.NominationError("Choose a file to upload.")
        services.add_document(n, m, form.cleaned_data["file"], actor=request.user)
        messages.success(request, "Document added.")
    return _member_action(request, nomination_id, act)


@require_GET
def download_document(request, org_slug, document_id):
    """Officials with view rights, or the nominee/submitter, may download."""
    document = get_object_or_404(NominationDocument.objects.select_related("nomination"),
                                 pk=document_id)
    nomination = document.nomination
    allowed = Perm.NOMINATIONS_VIEW in request.permissions
    if not allowed and Perm.NOMINATIONS_SUBMIT in request.permissions:
        member = services.member_for(request.user)
        allowed = member is not None and member.id in (nomination.nominee_id,
                                                       nomination.submitted_by_id)
    if not allowed:
        raise Http404
    if document.scan_status == NominationDocument.Scan.INFECTED:
        raise Http404
    response = FileResponse(default_storage.open(document.storage_name, "rb"),
                            as_attachment=True, filename=document.original_name,
                            content_type=document.mime)
    # Never rendered by the browser as part of this site.
    response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response


# --- Returning Officer ------------------------------------------------------------------

@require_GET
@requires(Perm.NOMINATIONS_VIEW)
def review_list(request, org_slug, election_id):
    election = _election(election_id)
    rules = election.current_rule_version.rules if election.current_rule_version else None
    rows = []
    for n in (Nomination.objects.filter(election=election)
              .select_related("nominee", "proposer", "seconder")
              .prefetch_related("documents").order_by("status", "nominee__full_name")):
        eligibility = evaluate(n.nominee, rules, request.org.timezone) if rules else None
        rows.append({"n": n, "eligibility": eligibility})
    return render(request, "nominations/review.html", {
        "election": election, "rows": rows,
        "candidates": Candidate.objects.filter(election=election),
        "can_decide": (Perm.NOMINATIONS_REVIEW in request.permissions
                       and election.state == lifecycle.REVIEW),
    })


@require_http_methods(["GET", "POST"])
@requires(Perm.NOMINATIONS_REVIEW)
@step_up_required
def decide(request, org_slug, nomination_id):
    nomination = _nomination(nomination_id)
    form = DecisionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.decide(nomination, approve=form.cleaned_data["decision"] == "approve",
                            reason=form.cleaned_data["reason"], actor=request.user)
        except services.NominationError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Decision recorded.")
            return redirect("nominations:review", org_slug=org_slug,
                            election_id=nomination.election_id)
    eligibility = (evaluate(nomination.nominee, nomination.election.current_rule_version.rules,
                            request.org.timezone)
                   if nomination.election.current_rule_version else None)
    return render(request, "nominations/decide.html", {
        "nomination": nomination, "form": form, "eligibility": eligibility,
        "documents": nomination.documents.all()})
