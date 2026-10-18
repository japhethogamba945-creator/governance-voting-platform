"""Checks to run after restoring a backup (docs/operations/backup-and-restore.md).

Run with an operator login that can SET ROLE app_rw and app_platform. Exits with an
error if any check fails.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from audit import chaining, checks
from elections.models import Election
from platform_core.db import acting_as, db_context
from platform_core.grants import APP_ROLES, privilege_differences
from tenancy.models import Organisation


class Command(BaseCommand):
    help = ("After a restore: check migrations, privileges, row-level security, every "
            "organisation's audit chain and every closed election's recorded fingerprints.")

    def handle(self, *args, **opts):
        problems: list[str] = []

        def check(ok: bool, label: str) -> None:
            self.stdout.write(f"{'OK  ' if ok else 'FAIL'} {label}")
            if not ok:
                problems.append(label)

        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        check(not pending, f"migrations applied ({len(pending)} pending)")
        missing, unexpected = privilege_differences()
        check(not missing and not unexpected,
              f"privileges match the manifest ({len(missing)} missing, "
              f"{len(unexpected)} unexpected)")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.relname FROM pg_class c JOIN information_schema.columns k "
                "ON k.table_name = c.relname AND k.column_name = 'organisation_id' "
                "AND k.table_schema = 'public' WHERE c.relkind = 'r' "
                "AND NOT (c.relrowsecurity AND c.relforcerowsecurity)")
            unprotected = [r[0] for r in cursor.fetchall()]
            cursor.execute("SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s) "
                           "AND (rolsuper OR rolbypassrls)", [list(APP_ROLES)])
            privileged = cursor.fetchone()[0]
        check(not unprotected, f"row-level security forced on every tenant table "
                               f"{unprotected or ''}".strip())
        check(privileged == 0, "no application role is superuser or bypasses RLS")

        with transaction.atomic(), acting_as("app_platform"):
            organisations = list(Organisation.objects.values_list("id", "slug"))
        for org_id, slug in organisations:
            with transaction.atomic(), acting_as("app_rw"), db_context(org_id=org_id):
                result, unchained = chaining.verify_org(org_id)
                check(result.ok, f"{slug}: audit chain ({result.events} events, "
                                 f"{result.anchors} anchors, {unchained} not yet chained)"
                      + ("" if result.ok else f": {'; '.join(result.problems[:3])}"))
                for election in (Election.objects.exclude(ballot_set_digest="")
                                 .select_related("current_rule_version")):
                    failed = [c.label for c in checks.election_checks(election)
                              if c.ok is False]
                    check(not failed, f"{slug}: election '{election.title}' fingerprints"
                                      + (f": {', '.join(failed)}" if failed else ""))
        if problems:
            raise CommandError(f"{len(problems)} post-restore check(s) failed.")
        self.stdout.write("All post-restore checks passed.")
