"""Ballot pages: ballot -> review -> cast -> receipt.

Choices are never stored between steps: the review page carries them in the
form itself. Request bodies are marked sensitive so error reports never include
them.
"""

import uuid

from django.conf import settings
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from accounts import throttle
from elections import lifecycle
from elections.models import Election
from electorate.models import Entitlement
from members.models import Member
from nominations.models import Candidate
from tenancy.permissions import requires
from tenancy.roles import Perm

from . import encoding, services

SENSITIVE = ("candidate", "receipt_commitment", "idempotency_key")


def _within_rate_limit(request) -> bool:
    """At most BALLOT_POSTS_PER_MINUTE review and cast submissions per member (05 §2)."""
    policy = throttle.Policy(threshold=settings.BALLOT_POSTS_PER_MINUTE, window_seconds=60,
                             exponential=False, max_lock_seconds=60)
    return throttle.allow(f"ballot:{request.user.id}", policy)


def _slow_down(request, election):
    response = render(request, "voting/slow_down.html", {"election": election}, status=429)
    response.headers["Retry-After"] = "60"
    return response


def _voter_context(request, election_id):
    election = get_object_or_404(
        Election.objects.select_related("current_rule_version"), pk=election_id)
    member = Member.objects.filter(user=request.user, status=Member.Status.ACTIVE).first()
    entitlement = (Entitlement.objects.filter(election=election, member=member).first()
                   if member else None)
    return election, member, entitlement


def _status_page(request, election, entitlement):
    """The right page for anything other than an issued entitlement in an open election."""
    if election.state not in (lifecycle.OPEN,) and not (
            entitlement and entitlement.status == Entitlement.Status.CONSUMED):
        return render(request, "voting/not_open.html", {"election": election})
    if entitlement is None:
        return render(request, "voting/not_eligible.html", {"election": election}, status=403)
    if entitlement.status == Entitlement.Status.CONSUMED:
        return render(request, "voting/already.html", {"election": election})
    if entitlement.status != Entitlement.Status.ISSUED:
        return render(request, "voting/not_eligible.html", {"election": election}, status=403)
    return None


def _ballot_page(request, election, *, selected=(), error=None, status=200):
    return render(request, "voting/ballot.html", {
        "election": election,
        "rules": election.current_rule_version.rules,
        "candidates": Candidate.objects.filter(election=election).order_by("ballot_order"),
        "selected": {str(s) for s in selected},
        "error": error,
    }, status=status)


@require_GET
@requires(Perm.ORG_VIEW)
def ballot(request, org_slug, election_id):
    election, _, entitlement = _voter_context(request, election_id)
    return _status_page(request, election, entitlement) or _ballot_page(request, election)


@sensitive_post_parameters(*SENSITIVE)
@require_POST
@requires(Perm.ORG_VIEW)
def review(request, org_slug, election_id):
    election, _, entitlement = _voter_context(request, election_id)
    if not _within_rate_limit(request):
        return _slow_down(request, election)
    page = _status_page(request, election, entitlement)
    if page is not None:
        return page
    selections = request.POST.getlist("candidate")
    candidates = {c.id: c for c in Candidate.objects.filter(election=election)}
    try:
        chosen = services.validate_selections(selections, set(candidates),
                                              election.current_rule_version.rules)
    except services.BallotRejected as exc:
        return _ballot_page(request, election, selected=selections, error=str(exc), status=400)
    chosen_candidates = sorted((candidates[c] for c in chosen), key=lambda c: c.ballot_order)
    return render(request, "voting/review.html", {
        "election": election,
        "chosen": chosen_candidates,
        "max": election.current_rule_version.rules["max_selections"],
        "idempotency_key": services.issue_idempotency_key(entitlement.id),
    })


@sensitive_post_parameters(*SENSITIVE)
@require_POST
@requires(Perm.ORG_VIEW)
def cast(request, org_slug, election_id):
    election, member, entitlement = _voter_context(request, election_id)
    if member is None or entitlement is None:
        return render(request, "voting/not_eligible.html", {"election": election}, status=403)
    if not _within_rate_limit(request):
        # Never a false "not recorded": a retry after a lost response must learn the truth.
        if entitlement.status == Entitlement.Status.CONSUMED:
            return render(request, "voting/already.html", {"election": election}, status=409)
        return _slow_down(request, election)
    commitment_hex = request.POST.get("receipt_commitment", "").strip().lower()
    commitment = None
    if commitment_hex:
        try:
            commitment = bytes.fromhex(commitment_hex)
        except ValueError:
            commitment = b"invalid"  # rejected by the service (wrong length)
    try:
        result = services.cast(
            election_id=election.id, member_id=member.id,
            selections=request.POST.getlist("candidate"),
            idempotency_key=request.POST.get("idempotency_key", ""),
            receipt_commitment=commitment, actor=request.user)
    except services.BallotRejected as exc:
        return render(request, "voting/rejected.html",
                      {"election": election, "reason": str(exc)}, status=400)
    if result.status == "already_voted":
        return render(request, "voting/already.html", {"election": election}, status=409)
    return render(request, "voting/receipt.html", {
        "election": election,
        "receipt_code": encoding.format_receipt_code(result.receipt_code)
        if result.receipt_code else "",
    })


@require_GET
@requires(Perm.MARKED_REGISTER_VIEW)
def marked_register(request, org_slug, election_id):
    """Who has voted (not how): Returning Officer and Auditor only (decision D6)."""
    try:
        uuid.UUID(str(election_id))
    except ValueError as exc:  # pragma: no cover - the URL converter already checks
        raise Http404 from exc
    election = get_object_or_404(Election.objects, pk=election_id)
    rows = (Entitlement.objects.filter(election=election).select_related("member")
            .order_by("member__full_name"))
    return render(request, "voting/marked_register.html", {
        "election": election, "rows": rows,
        "voted": sum(1 for r in rows if r.status == Entitlement.Status.CONSUMED),
    })
