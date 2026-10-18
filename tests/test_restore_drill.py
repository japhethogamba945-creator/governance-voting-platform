"""Acceptance test 16: backup restoration (docs/design/05-security-review.md §9).

A fictional election is run to a published result and the audit trail is
chained and anchored. The database is then backed up with ``pg_dump`` and
restored with ``pg_restore`` into a new database. On the restored copy:

* ``manage.py post_restore_check`` passes (migrations, privileges, row-level
  security, audit chain and anchors, every election's recorded fingerprints);
* the counting keys still work: a recount with two keyholders reproduces the
  original result exactly.
"""

import io
import os
import shutil
import subprocess
import uuid

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction

from audit.chaining import chain_and_anchor
from elections import lifecycle
from elections import services as election_services
from platform_core.db import acting_as
from tally import services as tally_services
from tally.models import Tally, TallyLine
from tenancy.roles import Role
from tests.test_stress_and_faults import _committed
from tests.test_tally import _as, _certify, _count, _election, _in_org, _vote_and_close


@pytest.fixture
def drill_world(django_db_setup, django_db_blocker):
    for w in _committed(django_db_blocker, voters=3):
        from tenancy.models import OrgMembership

        with acting_as(None):
            w["admin"] = (OrgMembership.all_objects
                          .filter(organisation=w["org"], roles__role="org_admin")
                          .select_related("user").first().user)
        yield w


def _pg(args: list[str]) -> None:
    db = connection.settings_dict
    env = {**os.environ, "PGPASSWORD": db["PASSWORD"] or ""}
    base = ["-h", db["HOST"] or "localhost", "-p", str(db["PORT"] or 5432), "-U", db["USER"]]
    subprocess.run([args[0], *base, *args[1:]], env=env, check=True,  # noqa: S603
                   capture_output=True, timeout=300)


def _lines(tally):
    with acting_as(None):
        return sorted((str(line.candidate_id), line.votes, line.outcome)
                      for line in TallyLine.all_objects.filter(tally=tally))


def test_backup_and_restore_drill(drill_world, tmp_path, settings):
    for tool in ("pg_dump", "pg_restore"):
        assert shutil.which(tool), f"{tool} is required for the restore drill"
    settings.AUDIT_ANCHOR_DIR = str(tmp_path / "anchors")
    w = drill_world
    _vote_and_close(w, [(0, 1), (0,), (1,)])
    original = _count(w)
    certification = _certify(w)
    with transaction.atomic(), _as(w, w["admin"]):
        tally_services.publish(_in_org(w), actor=w["admin"])
    chain_and_anchor()

    source = connection.settings_dict["NAME"]
    restored = f"{source}_restore_{uuid.uuid4().hex[:8]}"
    dump = tmp_path / "backup.dump"
    _pg(["pg_dump", "--format=custom", f"--file={dump}", source])
    with acting_as(None), connection.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE "{restored}"')
    try:
        _pg(["pg_restore", "--exit-on-error", f"--dbname={restored}", str(dump)])
        connection.close()
        connection.settings_dict["NAME"] = restored
        call_command("post_restore_check")

        assert _election(w).state == lifecycle.CERTIFIED
        with acting_as(None):
            copy = Tally.all_objects.get(pk=original.pk)
        assert _lines(copy) == _lines(original)
        # The keys survive the restore: two keyholders recount the restored ballot box.
        ro = w["ro"][0]
        with transaction.atomic(), _as(w, ro):
            election_services.transition(_in_org(w), lifecycle.DISPUTED, actor=ro,
                                         roles={Role.RETURNING_OFFICER},
                                         reason="Restore drill: recount on the restored copy")
        recount = _count(w, reason="Restore drill: recount on the restored copy")
        assert recount.version == 2 and _lines(recount) == _lines(original)
        assert recount.ballot_set_digest == original.ballot_set_digest
        with acting_as(None):
            certification.refresh_from_db()
        assert certification.superseded_at is not None  # only on the restored copy
        # The check bites: damage to the restored audit trail makes it fail.
        chain_and_anchor()
        with transaction.atomic(), acting_as(None), connection.cursor() as cursor:
            cursor.execute("SET LOCAL session_replication_role = replica")
            cursor.execute("UPDATE audit_event SET details = '{}' WHERE organisation_id = %s "
                           "AND action = 'tally.completed'", [w["org"].id])
            cursor.execute("SET LOCAL session_replication_role = origin")
        with pytest.raises(CommandError, match="post-restore check"):
            call_command("post_restore_check", stdout=io.StringIO())
    finally:
        connection.close()
        connection.settings_dict["NAME"] = source
        with acting_as(None), connection.cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS "{restored}" WITH (FORCE)')
    with acting_as(None):  # the source database was never touched by the drill
        certification.refresh_from_db()
        assert certification.superseded_at is None
        assert Tally.all_objects.filter(election=w["election"]).count() == 1
