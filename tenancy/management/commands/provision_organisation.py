from django.core.management.base import BaseCommand

from tenancy.services import provision_organisation


class Command(BaseCommand):
    help = "Create an organisation and invite its first administrator (Platform Owner)."

    def add_arguments(self, parser):
        parser.add_argument("--slug", required=True)
        parser.add_argument("--name", required=True, help="Legal name")
        parser.add_argument("--admin-email", required=True)
        parser.add_argument("--admin-name", default="")

    def handle(self, *args, **opts):
        org, user = provision_organisation(
            slug=opts["slug"], legal_name=opts["name"],
            admin_email=opts["admin_email"], admin_name=opts["admin_name"],
        )
        self.stdout.write(self.style.SUCCESS(
            f"Created organisation {org.slug} ({org.id}); first admin {user.email} invited."))
