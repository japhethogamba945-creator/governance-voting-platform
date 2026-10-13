import re

import pytest
from django.db import connection

from platform_core.grants import APP_ROLES, GRANTS

pytestmark = pytest.mark.django_db


def test_every_table_is_in_the_grants_manifest():
    with connection.cursor() as cursor:
        # pg_catalog, not information_schema: the latter hides tables app_rw cannot access.
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = {r[0] for r in cursor.fetchall()}
    assert tables - set(GRANTS) == set(), "add new tables to platform_core/grants.py"
    assert set(GRANTS) - tables == set(), "manifest lists tables that do not exist"


def _expected_table_privileges():
    expected = set()
    for table, roles in GRANTS.items():
        for role, privileges in roles.items():
            # Strip column lists: "UPDATE (a, b)" is a column privilege.
            table_level = re.sub(r"\w+\s*\([^)]*\)", "", privileges)
            for p in table_level.split(","):
                if p.strip():
                    expected.add((role, table, p.strip()))
    return expected


def test_table_privileges_match_manifest_exactly():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT a.grantee::regrole::text, c.relname, a.privilege_type "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, "
            "LATERAL aclexplode(c.relacl) a "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "AND a.grantee::regrole::text = ANY(%s)", [list(APP_ROLES)])
        actual = set(cursor.fetchall())
    assert actual == _expected_table_privileges()


def test_app_rw_cannot_update_user_email():
    with connection.cursor() as cursor:
        cursor.execute("SELECT has_column_privilege('app_rw', 'app_user', 'email', 'UPDATE')")
        assert cursor.fetchone()[0] is False
