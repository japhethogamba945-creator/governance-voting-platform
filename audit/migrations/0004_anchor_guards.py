from django.db import migrations

from platform_core.rls import tenant_rls

SQL = """
-- The chaining job works across organisations (as for audit_event).
CREATE POLICY chainer_all ON audit_anchor TO app_audit USING (true) WITH CHECK (true);

CREATE FUNCTION audit_anchor_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit anchors are append-only' USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER audit_anchor_immutable BEFORE UPDATE OR DELETE ON audit_anchor
    FOR EACH ROW EXECUTE FUNCTION audit_anchor_immutable();
CREATE TRIGGER audit_anchor_no_truncate BEFORE TRUNCATE ON audit_anchor
    FOR EACH STATEMENT EXECUTE FUNCTION audit_anchor_immutable();
"""

REVERSE_SQL = """
DROP TRIGGER audit_anchor_no_truncate ON audit_anchor;
DROP TRIGGER audit_anchor_immutable ON audit_anchor;
DROP FUNCTION audit_anchor_immutable();
DROP POLICY chainer_all ON audit_anchor;
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0003_anchors")]

    operations = [
        tenant_rls("audit_anchor"),
        migrations.RunSQL(sql=SQL, reverse_sql=REVERSE_SQL),
    ]
