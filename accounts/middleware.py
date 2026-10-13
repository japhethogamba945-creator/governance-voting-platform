import time

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login

# Paths reachable without a fully verified (password + second factor) login.
PUBLIC_PREFIXES = (
    "/accounts/login/",
    "/accounts/mfa/setup/",
    "/invite/",
    "/static/",
    "/healthz",
)


class SessionTimeoutMiddleware:
    """Idle and absolute session limits.

    Sets ``request.idle_seconds`` so stricter per-role limits (officials) can be
    applied once the organisation's roles are known.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.idle_seconds = 0.0
        if request.user.is_authenticated:
            now = time.time()
            last_seen = request.session.get("last_seen", now)
            login_at = request.session.get("login_at", now)
            request.idle_seconds = now - last_seen
            if (
                request.idle_seconds > settings.SESSION_IDLE_TIMEOUT
                or now - login_at > settings.SESSION_ABSOLUTE_TIMEOUT
            ):
                logout(request)
            else:
                request.session["last_seen"] = now
        return self.get_response(request)


class RequireVerifiedLoginMiddleware:
    """Every non-public path needs a session verified with the second factor."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if not path.startswith(PUBLIC_PREFIXES):
            user = request.user
            if not (user.is_authenticated and user.is_verified()):
                if user.is_authenticated:
                    logout(request)
                return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        return self.get_response(request)
