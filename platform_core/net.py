from django.conf import settings


def client_ip(request) -> str:
    """Client address, trusting exactly ``NUM_TRUSTED_PROXIES`` reverse proxies."""
    proxies = settings.NUM_TRUSTED_PROXIES
    if proxies:
        forwarded = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")]
        forwarded = [p for p in forwarded if p]
        if len(forwarded) >= proxies:
            return forwarded[-proxies]
    return request.META.get("REMOTE_ADDR", "")
