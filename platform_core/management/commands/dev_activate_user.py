from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from platform_core.db import acting_as


class Command(BaseCommand):
    help = ("DEVELOPMENT ONLY: set a password and activate an invited user, standing in for "
            "the invitation flow until increment 6.2. Refuses to run unless DEBUG is on.")

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("password")

    def handle(self, *args, email, password, **opts):
        if not settings.DEBUG:
            raise CommandError("dev_activate_user is disabled when DEBUG is off")
        User = get_user_model()
        with transaction.atomic(), acting_as(None):
            user = User.objects.get(email=User.objects.normalize_email(email))
            user.set_password(password)
            user.status = User.Status.ACTIVE
            user.save(update_fields=["password", "status"])
        self.stdout.write(self.style.SUCCESS(
            f"{user.email} activated; you will enrol an authenticator at first sign-in."))
