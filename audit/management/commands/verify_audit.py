import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from audit import chaining
from platform_core.db import acting_as, db_context
from tenancy.models import Organisation


class Command(BaseCommand):
    help = ("Recompute an organisation's audit hash chain and check it against its anchors "
            "and anchor files, or verify an exported bundle offline.")

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--org", help="Organisation slug.")
        group.add_argument("--bundle", help="Path to a downloaded verification bundle.")

    def handle(self, *args, org=None, bundle=None, **opts):
        if bundle:
            with open(bundle, encoding="utf-8") as handle:
                result = chaining.verify_bundle(json.load(handle))
            unchained = 0
        else:
            with acting_as(None):
                organisation = Organisation.objects.filter(slug=org).first()
            if organisation is None:
                raise CommandError(f"No organisation {org!r}.")
            with transaction.atomic(), db_context(org_id=organisation.id):
                result, unchained = chaining.verify_org(organisation.id)
        for problem in result.problems:
            self.stderr.write(f"PROBLEM: {problem}")
        self.stdout.write(f"{result.events} events, {result.anchors} anchors checked; "
                          f"head {result.head_seq} {result.head_hash}; "
                          f"{unchained} not yet chained.")
        if not result.ok:
            raise CommandError("The audit chain does NOT verify.")
        self.stdout.write("OK: the audit chain verifies.")
