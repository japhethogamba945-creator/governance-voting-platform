import uuid

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.db import transaction

from .db import db_context


def _session_user_id(request) -> uuid.UUID | None:
    raw = request.session.get(SESSION_KEY)
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


class RequestTransactionMiddleware:
    """One transaction per request, opened before authentication.

    ``app.user_id`` is taken from the server-side session so that loading the
    signed-in user already passes the ``app_user`` row-level security policy.
    Must sit directly after SessionMiddleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with transaction.atomic(), db_context(user_id=_session_user_id(request)):
            return self.get_response(request)


class SecurityHeadersMiddleware:
    """Headers not covered by Django's SecurityMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", settings.CONTENT_SECURITY_POLICY)
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            response.headers["Cache-Control"] = "no-store"
        return response
