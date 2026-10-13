import time
from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

SESSION_MFA_AT = "mfa_at"


def mark_mfa(request) -> None:
    request.session[SESSION_MFA_AT] = time.time()


def has_recent_mfa(request, max_age: int | None = None) -> bool:
    max_age = settings.STEP_UP_MAX_AGE if max_age is None else max_age
    return time.time() - request.session.get(SESSION_MFA_AT, 0) <= max_age


def step_up_redirect(request):
    query = urlencode({"next": request.get_full_path()})
    return redirect(f"{reverse('accounts:step_up')}?{query}")


def step_up_required(view):
    """Require the second factor within ``STEP_UP_MAX_AGE`` for privileged actions."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not has_recent_mfa(request):
            return step_up_redirect(request)
        return view(request, *args, **kwargs)

    return wrapper
