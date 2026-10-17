import uuid

from django import forms
from django.contrib import messages
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.stepup import step_up_required

from . import services
from .models import OrgMembership, RoleAssignment
from .permissions import requires
from .roles import GRANTABLE_ROLES, Perm, Role


@require_GET
def home(request):
    # Own memberships with an active role (own-membership and own-role RLS policies).
    memberships = (
        OrgMembership.all_objects.select_related("organisation")
        .filter(user=request.user)
        .filter(_active_roles_q(timezone.now()))
        .distinct()
        .order_by("organisation__legal_name")
    )
    return render(request, "tenancy/home.html", {"memberships": memberships})


@require_GET
@requires(Perm.ORG_VIEW)
def org_dashboard(request, org_slug):
    role_labels = [Role(r).label for r in sorted(request.roles)]
    return render(request, "tenancy/org_dashboard.html",
                  {"org": request.org, "role_labels": role_labels})


@require_GET
@requires(Perm.ORG_MANAGE_SETTINGS)
def org_settings(request, org_slug):
    return render(request, "tenancy/org_settings.html", {"org": request.org})


def _active_roles_q(now):
    return Q(roles__valid_to__isnull=True) | Q(roles__valid_to__gt=now)


@require_GET
@requires(Perm.ROLES_VIEW)
def roles_list(request, org_slug):
    now = timezone.now()
    assignments = (
        RoleAssignment.objects.select_related("membership__user")
        .filter(election_id__isnull=True, valid_from__lte=now)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now))
        .order_by("membership__user__email", "role")
    )
    people: dict = {}
    for a in assignments:
        entry = people.setdefault(a.membership_id, {"user": a.membership.user,
                                                    "membership_id": a.membership_id,
                                                    "roles": []})
        entry["roles"].append(a)
    return render(request, "tenancy/roles.html", {
        "people": sorted(people.values(), key=lambda p: p["user"].email),
        "grantable": [(r.value, r.label) for r in GRANTABLE_ROLES],
        "can_grant": Perm.ROLES_GRANT in request.permissions,
    })


@require_POST
@requires(Perm.ROLES_GRANT)
@step_up_required
def role_grant(request, org_slug):
    role = request.POST.get("role", "")
    membership = get_object_or_404(OrgMembership.objects, pk=_uuid(request.POST.get("membership")))
    try:
        services.grant_role(membership, role, actor=request.user,
                            actor_membership=request.membership)
    except services.RoleError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{Role(role).label} role granted.")
    return redirect("tenancy:roles", org_slug=org_slug)


@require_POST
@requires(Perm.ROLES_GRANT)
@step_up_required
def role_revoke(request, org_slug):
    assignment = get_object_or_404(RoleAssignment.objects,
                                   pk=_uuid(request.POST.get("assignment")))
    try:
        services.revoke_role(assignment, actor=request.user, actor_membership=request.membership)
    except services.RoleError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{Role(assignment.role).label} role removed.")
    return redirect("tenancy:roles", org_slug=org_slug)


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise Http404 from exc


class OfficialInviteForm(forms.Form):
    email = forms.EmailField(max_length=254, label="Email")
    full_name = forms.CharField(max_length=200, label="Full name")
    role = forms.ChoiceField(choices=[(r.value, r.label) for r in GRANTABLE_ROLES],
                             label="Role")


@require_http_methods(["GET", "POST"])
@requires(Perm.ROLES_GRANT)
@step_up_required
def official_invite(request, org_slug):
    """Appoint an official (e.g. an independent Returning Officer) who need not be a member."""
    from members.services import MemberError, invite_official

    form = OfficialInviteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            invite_official(email=form.cleaned_data["email"],
                            full_name=form.cleaned_data["full_name"],
                            role=form.cleaned_data["role"], actor=request.user,
                            actor_membership=request.membership)
        except MemberError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Invitation sent to {form.cleaned_data['email']}.")
            return redirect("tenancy:roles", org_slug=org_slug)
    return render(request, "tenancy/official_invite.html", {"form": form})
