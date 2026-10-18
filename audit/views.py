"""Audit trail, chain verification, offline bundle and election checks."""

import json
import uuid

from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_http_methods

from elections.models import Election
from tenancy.permissions import requires
from tenancy.roles import Perm

from . import chaining, checks
from .models import AuditAnchor, AuditEvent

# Who voted is the marked register: only for those who may see it (decision D6).
REGISTER_ACTIONS = ("ballot.accepted",)


def _events(request):
    events = (AuditEvent.objects.filter(organisation_id=request.org.id)
              .select_related("actor").order_by("-at", "-seq"))
    if Perm.MARKED_REGISTER_VIEW not in request.permissions:
        events = events.exclude(action__in=REGISTER_ACTIONS)
    election = request.GET.get("election", "")
    try:
        if election:
            events = events.filter(object_id=uuid.UUID(election))
    except ValueError:
        election = ""
    action = request.GET.get("action", "").strip()[:64]
    if action:
        events = events.filter(action__startswith=action)
    return events, election, action


@require_http_methods(["GET", "POST"])
@requires(Perm.AUDIT_VIEW)
def trail(request, org_slug):
    events, election, action = _events(request)
    page = Paginator(events, 100).get_page(request.GET.get("page"))
    verification = unchained = None
    if request.method == "POST":
        verification, unchained = chaining.verify_org(request.org.id)
    return render(request, "audit/trail.html", {
        "page": page, "election": election, "action": action,
        "elections": Election.objects.order_by("-created_at"),
        "anchors": AuditAnchor.objects.filter(organisation_id=request.org.id)[:10],
        "verification": verification, "unchained": unchained,
        "filtered": Perm.MARKED_REGISTER_VIEW not in request.permissions,
        "can_download": Perm.MARKED_REGISTER_VIEW in request.permissions,
    })


@require_GET
@requires(Perm.AUDIT_VIEW)
def bundle(request, org_slug):
    if Perm.MARKED_REGISTER_VIEW not in request.permissions:
        raise PermissionDenied  # the bundle includes who voted (decision D6)
    body = json.dumps(chaining.bundle(request.org), ensure_ascii=False, indent=1)
    response = HttpResponse(body.encode(), content_type="application/json; charset=utf-8")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="audit-{request.org.slug}.json"')
    return response


@require_GET
@requires(Perm.TALLY_VIEW)
def election_checks(request, org_slug, election_id):
    election = get_object_or_404(Election.objects.select_related("current_rule_version"),
                                 pk=election_id)
    results = checks.election_checks(election)
    verification, unchained = chaining.verify_org(request.org.id)
    return render(request, "audit/election_checks.html", {
        "election": election, "checks": results, "verification": verification,
        "unchained": unchained,
        "all_ok": all(c.ok is not False for c in results) and verification.ok,
    })
