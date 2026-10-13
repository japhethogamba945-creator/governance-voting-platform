from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.urls import Resolver404, resolve
from django.utils import timezone

from platform_core.db import db_context

from .models import Organisation, OrgMembership, RoleAssignment
from .roles import OFFICIAL_ROLES, permissions_for


class TenantContextMiddleware:
    """Runs each request in one transaction carrying the RLS context.

    For ``/o/<org_slug>/...`` URLs the organisation is resolved from the slug
    *and* the user's membership (never from form input). Non-members and unknown
    organisations get the same 404, so organisations cannot be discovered.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            org_slug = resolve(request.path_info).kwargs.get("org_slug")
        except Resolver404:
            org_slug = None
        user_id = request.user.pk if request.user.is_authenticated else None

        with transaction.atomic(), db_context(user_id=user_id):
            if org_slug is None:
                return self.get_response(request)
            if user_id is None:  # normally caught earlier by RequireVerifiedLoginMiddleware
                raise Http404
            membership = (
                OrgMembership.all_objects.select_related("organisation")
                .filter(user_id=user_id, organisation__slug=org_slug)
                .first()
            )
            if membership is None:
                raise Http404
            org = membership.organisation
            with db_context(user_id=user_id, org_id=org.id):
                now = timezone.now()
                roles = set(
                    RoleAssignment.objects.filter(
                        membership=membership, election_id__isnull=True, valid_from__lte=now
                    )
                    .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now))
                    .values_list("role", flat=True)
                )
                if roles & OFFICIAL_ROLES and request.idle_seconds > settings.OFFICIAL_IDLE_TIMEOUT:
                    logout(request)
                    return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
                if org.status != Organisation.Status.ACTIVE:
                    raise PermissionDenied("This organisation is suspended.")
                request.org = org
                request.membership = membership
                request.roles = frozenset(roles)
                request.permissions = permissions_for(roles)
                return self.get_response(request)
