from django.conf import settings


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
