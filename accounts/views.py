import base64
import io
import time

import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST
from django_otp import devices_for_user, match_token
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice

from audit.services import record as audit
from platform_core.db import lookup_scope
from platform_core.net import client_ip

from . import throttle
from .forms import LoginForm, TokenForm
from .models import RecoveryCodeDevice
from .stepup import mark_mfa

GENERIC_LOGIN_ERROR = "Email or password is incorrect."
GENERIC_CODE_ERROR = "That code is not valid. Check your authenticator app and try again."
LOCKED_ERROR = "Too many attempts. Please wait a few minutes and try again."

PRE_MFA_USER = "pre_mfa_user_id"
PRE_MFA_EMAIL = "pre_mfa_email"
PRE_MFA_AT = "pre_mfa_at"
PRE_MFA_NEXT = "pre_mfa_next"
SHOW_RECOVERY = "show_recovery_codes"


def _safe_next(request, candidate: str | None) -> str:
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return reverse(settings.LOGIN_REDIRECT_URL)


def _pending_user(request):
    """The user who passed the password step but not yet the second factor."""
    user_id = request.session.get(PRE_MFA_USER)
    email = request.session.get(PRE_MFA_EMAIL)
    started = request.session.get(PRE_MFA_AT, 0)
    if not user_id or not email or time.time() - started > settings.PRE_MFA_MAX_AGE:
        return None
    with lookup_scope(email=email):
        user = get_user_model().objects.filter(pk=user_id, email=email).first()
    return user if user is not None and user.is_active else None


def start_pre_mfa(request, user, next_url: str) -> None:
    """Record that ``user`` passed the first factor; the second factor comes next."""
    request.session.cycle_key()
    request.session[PRE_MFA_USER] = str(user.pk)
    request.session[PRE_MFA_EMAIL] = user.email
    request.session[PRE_MFA_AT] = time.time()
    request.session[PRE_MFA_NEXT] = next_url


def _has_confirmed_device(user) -> bool:
    return any(True for _ in devices_for_user(user, confirmed=True))


def _complete_login(request, user, device):
    next_url = request.session.get(PRE_MFA_NEXT)
    for key in (PRE_MFA_USER, PRE_MFA_EMAIL, PRE_MFA_AT, PRE_MFA_NEXT):
        request.session.pop(key, None)
    with lookup_scope(email=user.email):  # login() updates app_user.last_login
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    otp_login(request, device)
    now = time.time()
    request.session["login_at"] = now
    request.session["last_seen"] = now
    mark_mfa(request)
    throttle.reset(throttle.account_key(user.email))
    audit("auth.login", actor=user, details={"method": type(device).__name__})
    return next_url


@sensitive_post_parameters("password")
@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated and request.user.is_verified():
        return redirect(settings.LOGIN_REDIRECT_URL)
    form = LoginForm(request.POST or None)
    error = None
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].strip().lower()
        ip = client_ip(request)
        if throttle.is_locked(throttle.account_key(email), throttle.ip_key(ip)):
            error = LOCKED_ERROR
        else:
            with lookup_scope(email=email):
                user = authenticate(request, email=email, password=form.cleaned_data["password"])
            if user is None:
                throttle.record_login_failure(email, ip)
                error = GENERIC_LOGIN_ERROR
            else:
                start_pre_mfa(request, user, _safe_next(request, request.GET.get("next")))
                if _has_confirmed_device(user):
                    return redirect("accounts:login_verify")
                return redirect("accounts:mfa_setup")
    return render(request, "accounts/login.html", {"form": form, "error": error},
                  status=200 if error is None else 400)


@sensitive_post_parameters("token")
@require_http_methods(["GET", "POST"])
def login_verify(request):
    user = _pending_user(request)
    if user is None:
        return redirect("accounts:login")
    if not _has_confirmed_device(user):
        return redirect("accounts:mfa_setup")
    form = TokenForm(request.POST or None)
    error = None
    if request.method == "POST" and form.is_valid():
        ip = client_ip(request)
        if throttle.is_locked(throttle.account_key(user.email), throttle.ip_key(ip)):
            error = LOCKED_ERROR
        else:
            device = match_token(user, form.cleaned_data["token"])
            if device is None:
                throttle.record_login_failure(user.email, ip)
                error = GENERIC_CODE_ERROR
            else:
                next_url = _complete_login(request, user, device)
                return redirect(_safe_next(request, next_url))
    return render(request, "accounts/login_verify.html", {"form": form, "error": error},
                  status=200 if error is None else 400)


def _qr_data_uri(text: str) -> str:
    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buffer = io.BytesIO()
    image.save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()


@sensitive_post_parameters("token")
@require_http_methods(["GET", "POST"])
def mfa_setup(request):
    """First-time authenticator enrolment, reachable only between password and MFA."""
    user = _pending_user(request)
    if user is None:
        return redirect("accounts:login")
    if _has_confirmed_device(user):
        return redirect("accounts:login_verify")
    device = TOTPDevice.objects.filter(user=user, confirmed=False).first()
    if device is None:
        device = TOTPDevice.objects.create(user=user, name="Authenticator app", confirmed=False)
    # Reuse the already-loaded user: before sign-in completes, app_user RLS hides
    # the row from a fresh lazy lookup (device.config_url reads device.user).
    device.user = user
    form = TokenForm(request.POST or None)
    error = None
    if request.method == "POST" and form.is_valid():
        ip = client_ip(request)
        if throttle.is_locked(throttle.account_key(user.email), throttle.ip_key(ip)):
            error = LOCKED_ERROR
        elif device.verify_token(form.cleaned_data["token"]):
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            recovery, _ = RecoveryCodeDevice.objects.get_or_create(
                user=user, defaults={"name": "Recovery codes", "confirmed": True}
            )
            codes = recovery.generate_codes()
            audit("auth.mfa_enrolled", actor=user, details={"device": "totp"})
            next_url = _complete_login(request, user, device)
            request.session[SHOW_RECOVERY] = codes
            request.session[PRE_MFA_NEXT] = next_url
            return redirect("accounts:recovery_codes")
        else:
            throttle.record_login_failure(user.email, ip)
            error = GENERIC_CODE_ERROR
    secret = base64.b32encode(device.bin_key).decode()
    return render(request, "accounts/mfa_setup.html", {
        "form": form,
        "error": error,
        "qr": _qr_data_uri(device.config_url),
        "secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
    }, status=200 if error is None else 400)


@require_http_methods(["GET", "POST"])
def recovery_codes(request):
    """Shows freshly generated recovery codes exactly once."""
    if request.method == "POST":
        request.session.pop(SHOW_RECOVERY, None)
        return redirect(_safe_next(request, request.session.pop(PRE_MFA_NEXT, None)))
    codes = request.session.get(SHOW_RECOVERY)
    if not codes:
        return redirect(settings.LOGIN_REDIRECT_URL)
    return render(request, "accounts/recovery_codes.html", {"codes": codes})


@sensitive_post_parameters("token")
@require_http_methods(["GET", "POST"])
def step_up(request):
    form = TokenForm(request.POST or None)
    error = None
    next_url = _safe_next(request, request.GET.get("next") or request.POST.get("next"))
    if request.method == "POST" and form.is_valid():
        user = request.user
        ip = client_ip(request)
        if throttle.is_locked(throttle.account_key(user.email), throttle.ip_key(ip)):
            error = LOCKED_ERROR
        elif match_token(user, form.cleaned_data["token"]) is not None:
            mark_mfa(request)
            audit("auth.step_up", actor=user)
            return redirect(next_url)
        else:
            throttle.record_login_failure(user.email, ip)
            error = GENERIC_CODE_ERROR
    return render(request, "accounts/step_up.html",
                  {"form": form, "error": error, "next": next_url},
                  status=200 if error is None else 400)


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        audit("auth.logout", actor=request.user)
    logout(request)
    return redirect("accounts:login")
