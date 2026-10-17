from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from accounts.stepup import has_recent_mfa, step_up_redirect
from tenancy.permissions import requires
from tenancy.roles import Perm

from . import lifecycle, rules, services
from .forms import ElectionForm, MeetingForm, RulesForm, TransitionForm
from .models import Election, Meeting


def _election(election_id) -> Election:
    return get_object_or_404(
        Election.objects.select_related("meeting", "current_rule_version",
                                        "organisation"), pk=election_id)


# --- meetings -----------------------------------------------------------------

@require_GET
@requires(Perm.ELECTIONS_VIEW)
def meeting_list(request, org_slug):
    return render(request, "elections/meeting_list.html", {
        "meetings": Meeting.objects.all(),
        "can_manage": Perm.MEETINGS_MANAGE in request.permissions,
    })


@require_http_methods(["GET", "POST"])
@requires(Perm.MEETINGS_MANAGE)
def meeting_create(request, org_slug):
    form = MeetingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        services.create_meeting(form.save(commit=False), actor=request.user)
        messages.success(request, "Meeting created.")
        return redirect("elections:meeting_list", org_slug=org_slug)
    return render(request, "elections/meeting_form.html", {"form": form, "creating": True})


@require_http_methods(["GET", "POST"])
@requires(Perm.MEETINGS_MANAGE)
def meeting_edit(request, org_slug, meeting_id):
    meeting = get_object_or_404(Meeting.objects, pk=meeting_id)
    form = MeetingForm(request.POST or None, instance=meeting)
    if request.method == "POST" and form.is_valid():
        services.save_meeting(form.save(commit=False), actor=request.user)
        messages.success(request, "Meeting updated.")
        return redirect("elections:meeting_list", org_slug=org_slug)
    return render(request, "elections/meeting_form.html", {"form": form, "meeting": meeting})


# --- elections ----------------------------------------------------------------

@require_GET
@requires(Perm.ELECTIONS_VIEW)
def election_list(request, org_slug):
    return render(request, "elections/election_list.html", {
        "elections": Election.objects.select_related("meeting"),
        "can_edit": Perm.ELECTIONS_EDIT in request.permissions,
    })


@require_http_methods(["GET", "POST"])
@requires(Perm.ELECTIONS_EDIT)
def election_create(request, org_slug):
    form = ElectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            election = services.create_election(form.save(commit=False), actor=request.user)
        except services.ElectionError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Election created as a draft. Next, set its rules.")
            return redirect("elections:detail", org_slug=org_slug, election_id=election.id)
    return render(request, "elections/election_form.html", {"form": form, "creating": True})


@require_GET
@requires(Perm.ELECTIONS_VIEW)
def election_detail(request, org_slug, election_id):
    election = _election(election_id)
    version = election.current_rule_version
    return render(request, "elections/election_detail.html", {
        "election": election,
        "version": version,
        "summary": rules.describe(version.rules, election.seats) if version else [],
        "versions": election.rule_versions.select_related("created_by", "approved_by"),
        "history": election.transitions.select_related("actor"),
        "actions": lifecycle.available_for(election.state, request.roles),
        "key_shares": election.key_shares.all(),
        "electorate_size": election.electorate.count(),
        "votes_cast": election.entitlements.filter(status="consumed").count(),
        "editable": (election.state in lifecycle.EDITABLE_STATES
                     and Perm.ELECTIONS_EDIT in request.permissions),
    })


@require_http_methods(["GET", "POST"])
@requires(Perm.ELECTIONS_EDIT)
def election_edit(request, org_slug, election_id):
    election = _election(election_id)
    if election.state not in lifecycle.EDITABLE_STATES:
        messages.error(request, "The configuration is locked in the election's current state.")
        return redirect("elections:detail", org_slug=org_slug, election_id=election.id)
    before = services.snapshot(election)
    form = ElectionForm(request.POST or None, instance=election)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_election(form.save(commit=False), actor=request.user, before=before)
        except services.ElectionError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Election updated.")
            return redirect("elections:detail", org_slug=org_slug, election_id=election.id)
    return render(request, "elections/election_form.html", {"form": form, "election": election})


@require_http_methods(["GET", "POST"])
@requires(Perm.ELECTIONS_EDIT)
def election_rules(request, org_slug, election_id):
    election = _election(election_id)
    if election.state not in lifecycle.EDITABLE_STATES:
        messages.error(request, "The rules are locked in the election's current state.")
        return redirect("elections:detail", org_slug=org_slug, election_id=election.id)
    form = RulesForm(request.POST or None, election=election)
    if request.method == "POST" and form.is_valid():
        try:
            version = services.save_rules(election, form.to_rules(), actor=request.user)
        except services.ElectionError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Rules saved as version {version.version}.")
            return redirect("elections:detail", org_slug=org_slug, election_id=election.id)
    return render(request, "elections/election_rules.html", {"form": form, "election": election})


@require_http_methods(["GET", "POST"])
@requires(Perm.ELECTIONS_VIEW)
def election_transition(request, org_slug, election_id, target):
    election = _election(election_id)
    spec = lifecycle.TRANSITIONS.get((election.state, target))
    if spec is None or not spec.available or not (spec.roles & request.roles):
        messages.error(request, "That action is not available.")
        return redirect("elections:detail", org_slug=org_slug, election_id=election.id)
    if spec.step_up and not has_recent_mfa(request):
        return step_up_redirect(request)  # then back to this confirmation page
    keyholder_choices = None
    actor_is_keyholder = True
    if target == lifecycle.SCHEDULED:
        from keys.services import active_key, eligible_keyholders

        actor_is_keyholder = active_key(request.user) is not None
        keyholder_choices = [(str(k.user_id), k.user.full_name or k.user.email)
                             for k in eligible_keyholders().exclude(user=request.user)]
    # Bind on any POST: an empty POST (no note typed) is falsy but still a submission.
    form = TransitionForm(request.POST if request.method == "POST" else None,
                          reason_required=spec.reason_required,
                          keyholder_choices=keyholder_choices)
    if request.method == "POST" and form.is_valid():
        context = {"keyholder_user_ids": form.cleaned_data.get("keyholders", [])}
        try:
            services.transition(election, target, actor=request.user, roles=request.roles,
                                reason=form.cleaned_data["reason"], context=context)
        except services.ElectionError as exc:
            form.add_error(None, str(exc))
        else:
            if "backup_secret" in context:
                election.refresh_from_db(fields=["public_key", "public_key_fpr", "state"])
                # Rendered directly (not stored, not redirected): shown exactly once.
                response = render(request, "elections/backup_code.html", {
                    "election": election, "backup_code": context["backup_secret"]})
                response.headers["Cache-Control"] = "no-store"
                return response
            messages.success(request, f"{spec.label}: done.")
            return redirect("elections:detail", org_slug=org_slug, election_id=election.id)
    version = election.current_rule_version
    return render(request, "elections/election_transition.html", {
        "election": election, "spec": spec, "form": form,
        "actor_is_keyholder": actor_is_keyholder,
        "summary": rules.describe(version.rules, election.seats) if version else [],
        "version": version,
    })
