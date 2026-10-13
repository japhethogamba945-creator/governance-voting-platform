from django.shortcuts import render
from django.views.decorators.http import require_GET

from .models import OrgMembership
from .permissions import requires
from .roles import Perm, Role


@require_GET
def home(request):
    # Own memberships only: enforced by the own-membership RLS policy.
    memberships = (
        OrgMembership.all_objects.select_related("organisation")
        .filter(user=request.user)
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
