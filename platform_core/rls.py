"""Migration helpers for Row-Level Security and composite foreign keys.

Every table with an ``organisation_id`` column must call :func:`tenant_rls`;
``tests/test_rls_catalogue.py`` fails the build if one is missed.
"""

import re

from django.db import migrations

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def tenant_rls(table: str) -> migrations.RunSQL:
    """Enable + force RLS with the standard ``tenant_isolation`` policy."""
    t = _ident(table)
    return migrations.RunSQL(
        sql=f"""
            ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {t}
                USING (organisation_id = current_org_id())
                WITH CHECK (organisation_id = current_org_id());
        """,
        reverse_sql=f"""
            DROP POLICY IF EXISTS tenant_isolation ON {t};
            ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY;
            ALTER TABLE {t} DISABLE ROW LEVEL SECURITY;
        """,
    )


def composite_fk(
    table: str, name: str, columns: list[str], ref_table: str, ref_columns: list[str]
) -> migrations.RunSQL:
    """Add a composite foreign key, e.g. ``(organisation_id, x_id) -> x(organisation_id, id)``.

    This makes a reference to another organisation's row structurally
    impossible, independently of RLS.
    """
    cols = ", ".join(_ident(c) for c in columns)
    ref_cols = ", ".join(_ident(c) for c in ref_columns)
    return migrations.RunSQL(
        sql=(
            f"ALTER TABLE {_ident(table)} ADD CONSTRAINT {_ident(name)} "
            f"FOREIGN KEY ({cols}) REFERENCES {_ident(ref_table)} ({ref_cols}) "
            "DEFERRABLE INITIALLY IMMEDIATE"
        ),
        reverse_sql=f"ALTER TABLE {_ident(table)} DROP CONSTRAINT {_ident(name)}",
    )
