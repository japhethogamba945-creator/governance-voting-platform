"""Print the current authenticator codes of the demo accounts (development only).

Stands in for an authenticator app while clicking through the demo. Only accounts
with an @demo.example address are shown, and only with DEBUG on.
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from platform_core.db import acting_as

from .load_demo import DEMO_DOMAIN


class Command(BaseCommand):
    help = "Show the current 6-digit codes for the demo accounts (DEBUG only)."

    def add_arguments(self, parser):
        parser.add_argument("--slug", help="Only the people of this demo organisation.")

    def handle(self, *args, slug=None, **opts):
        if not settings.DEBUG:
            raise CommandError("demo_codes only runs with DEBUG on (development).")
        with acting_as(None):
            devices = TOTPDevice.objects.filter(user__email__endswith=f"@{DEMO_DOMAIN}")
            if slug:
                devices = devices.filter(user__memberships__organisation__slug=slug)
            devices = list(devices.select_related("user").order_by("user__email"))
        if not devices:
            raise CommandError("No demo accounts. Run: python manage.py load_demo")
        remaining = 30 - int(time.time()) % 30
        for device in devices:
            totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
            self.stdout.write(f"{device.user.email:<32} {totp.token():0{device.digits}d}")
        self.stdout.write(f"\nValid for about {remaining} more seconds. Each code works once.")
