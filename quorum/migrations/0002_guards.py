from django.db import migrations

from platform_core.rls import composite_fk, tenant_rls

SQL = """
-- Attendance can be recorded or corrected from the opening of voting until
-- certification, and only for members of the frozen electorate.
CREATE FUNCTION participation_record_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    r participation_record%ROWTYPE;
    st text;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'participation records cannot be changed; remove and add instead'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    r := CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    SELECT state INTO st FROM election WHERE id = r.election_id;
    IF st IS NULL OR st NOT IN ('open', 'closed', 'counted', 'awaiting_certification') THEN
        RAISE EXCEPTION 'attendance can only change between opening and certification (%)', st
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'INSERT' AND NOT EXISTS (
            SELECT 1 FROM electorate_entry e
            WHERE e.election_id = NEW.election_id AND e.member_id = NEW.member_id) THEN
        RAISE EXCEPTION 'only members of the electorate can be recorded as attending'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
CREATE TRIGGER participation_record_guard BEFORE INSERT OR UPDATE OR DELETE
    ON participation_record FOR EACH ROW EXECUTE FUNCTION participation_record_guard();
"""

REVERSE_SQL = """
DROP TRIGGER participation_record_guard ON participation_record;
DROP FUNCTION participation_record_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("quorum", "0001_initial"),
        ("electorate", "0003_stage_requirements"),
    ]

    operations = [
        tenant_rls("participation_record"),
        composite_fk("participation_record", "participation_record_election_same_org",
                     ["organisation_id", "election_id"], "election", ["organisation_id", "id"]),
        composite_fk("participation_record", "participation_record_member_same_org",
                     ["organisation_id", "member_id"], "member", ["organisation_id", "id"]),
        migrations.RunSQL(sql=SQL, reverse_sql=REVERSE_SQL),
    ]
