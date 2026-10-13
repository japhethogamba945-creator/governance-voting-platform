"""Transactional emails. Links use SITE_URL, never the request's Host header."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse


def _absolute(path: str) -> str:
    if not settings.SITE_URL:
        raise ImproperlyConfigured("SITE_URL must be set to send emails with links")
    return settings.SITE_URL.rstrip("/") + path


def send_invitation(member, token: str, expires_at) -> None:
    context = {
        "member": member,
        "org": member.organisation,
        "link": _absolute(reverse("invite_start", kwargs={"token": token})),
        "expires_at": expires_at,
    }
    send_mail(
        subject=f"Set up your account for {member.organisation.legal_name}",
        message=render_to_string("members/email/invitation.txt", context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[member.email],
    )


def send_added_to_organisation(member) -> None:
    context = {
        "member": member,
        "org": member.organisation,
        "link": _absolute(reverse("accounts:login")),
    }
    send_mail(
        subject=f"You have been added to {member.organisation.legal_name}",
        message=render_to_string("members/email/added.txt", context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[member.email],
    )
