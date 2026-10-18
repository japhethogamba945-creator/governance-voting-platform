import uuid

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from accounts.stepup import step_up_required
from elections.models import Election
from tenancy.permissions import requires
from tenancy.roles import Perm

from . import services
from .models import ParticipationRecord


def _election(election_id) -> Election:
    return get_object_or_404(Election.objects.select_related("current_rule_version"),
                             pk=election_id)


def _back(org_slug, election):
    return redirect("quorum:attendance", org_slug=org_slug, election_id=election.id)


@require_GET
@requires(Perm.TALLY_VIEW)
def attendance(request, org_slug, election_id):
    election = _election(election_id)
    rule = (election.current_rule_version.rules.get("quorum") if election.current_rule_version
            else None) or {"type": "none"}
    return render(request, "quorum/attendance.html", {
        "election": election,
        "rule": rule,
        "records": ParticipationRecord.objects.filter(election=election)
                   .select_related("member", "recorded_by"),
        "editable": (Perm.TALLY_RUN in request.permissions
                     and election.state in services.ATTENDANCE_STATES),
    })


@require_POST
@requires(Perm.TALLY_RUN)
@step_up_required
def add(request, org_slug, election_id):
    election = _election(election_id)
    numbers = request.POST.get("member_numbers", "").replace(",", "\n").splitlines()
    try:
        added, unknown = services.record_attendance(election, numbers, actor=request.user)
    except services.QuorumError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{added} attendance record{'s' if added != 1 else ''} added.")
        if unknown:
            messages.error(request, "Not in this election's electorate: "
                                    + ", ".join(unknown[:20]))
    return _back(org_slug, election)


@require_POST
@requires(Perm.TALLY_RUN)
@step_up_required
def remove(request, org_slug, election_id):
    election = _election(election_id)
    try:
        record_id = uuid.UUID(request.POST.get("record", ""))
    except ValueError:
        messages.error(request, "Choose a record to remove.")
        return _back(org_slug, election)
    try:
        services.remove_attendance(election, record_id, actor=request.user)
    except services.QuorumError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Attendance record removed.")
    return _back(org_slug, election)
