from django.db import migrations

SQL = """
-- Stage requirements that depend on the key and electorate tables.
CREATE FUNCTION election_stage_requirements() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'scheduled' AND OLD.state = 'ready_for_approval' THEN
        IF NEW.public_key IS NULL OR NEW.public_key_fpr = '' THEN
            RAISE EXCEPTION 'scheduling requires the key ceremony (no public key)'
                USING ERRCODE = 'check_violation';
        END IF;
        IF (SELECT count(*) FROM key_share s WHERE s.election_id = NEW.id) < 3 THEN
            RAISE EXCEPTION 'scheduling requires the key shares from the key ceremony'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.state = 'open' AND OLD.state = 'scheduled' THEN
        IF NEW.electorate_digest = ''
           OR NOT EXISTS (SELECT 1 FROM entitlement t WHERE t.election_id = NEW.id) THEN
            RAISE EXCEPTION 'opening requires the frozen electorate and its entitlements'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.opens_at > now() THEN
            RAISE EXCEPTION 'the opening time has not been reached'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER election_stage_requirements BEFORE UPDATE OF state ON election
    FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state)
    EXECUTE FUNCTION election_stage_requirements();

-- The scheduler lists elections due to open or close across organisations,
-- only while it has set app.scheduler (platform_core.db.scheduler_scope).
CREATE POLICY scheduler_due ON election FOR SELECT TO app_rw USING (
    current_setting('app.scheduler', true) = 'on' AND state IN ('scheduled', 'open'));
"""

REVERSE_SQL = """
DROP POLICY scheduler_due ON election;
DROP TRIGGER election_stage_requirements ON election;
DROP FUNCTION election_stage_requirements();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("electorate", "0002_rls_and_guards"),
        ("keys", "0002_rls_and_guards"),
    ]

    operations = [migrations.RunSQL(sql=SQL, reverse_sql=REVERSE_SQL)]
