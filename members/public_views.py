"""Invitation acceptance: reachable without signing in (token-gated)."""

from django.contrib.auth import logout
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods

from accounts.views import start_pre_mfa

from . import services
from .forms import AcceptInvitationForm

INVITE_SESSION_KEY = "pending_invitation"


def _invalid(request):
    return render(request, "members/invite_invalid.html", status=410)


@sensitive_variables("token")
@require_GET
def invite_start(request, token):
    """Move the token from the URL into the session so it leaves the address bar
    and browser history straight away."""
    if services.find_open_invitation(token) is None:
        return _invalid(request)
    if request.user.is_authenticated:  # only a valid invitation ends the current session
        logout(request)
    request.session.cycle_key()
    request.session[INVITE_SESSION_KEY] = token
    return redirect("invite_accept")


@sensitive_post_parameters("password1", "password2")
@require_http_methods(["GET", "POST"])
def invite_accept(request):
    token = request.session.get(INVITE_SESSION_KEY, "")
    invitation = services.find_open_invitation(token)
    if invitation is None:
        request.session.pop(INVITE_SESSION_KEY, None)
        return _invalid(request)
    if invitation.user.status != invitation.user.Status.INVITED:
        # Account already set up (e.g. invited by another organisation first).
        services.accept_invitation(token)
        request.session.pop(INVITE_SESSION_KEY, None)
        return redirect("accounts:login")
    form = AcceptInvitationForm(request.POST or None, user=invitation.user)
    if request.method == "POST" and form.is_valid():
        invitation = services.accept_invitation(token, form.cleaned_data["password1"])
        request.session.pop(INVITE_SESSION_KEY, None)
        dashboard = reverse("tenancy:dashboard",
                            kwargs={"org_slug": invitation.organisation.slug})
        start_pre_mfa(request, invitation.user, dashboard)
        return redirect("accounts:mfa_setup")
    return render(request, "members/invite_accept.html", {
        "form": form, "org": invitation.organisation, "email": invitation.user.email,
    })
