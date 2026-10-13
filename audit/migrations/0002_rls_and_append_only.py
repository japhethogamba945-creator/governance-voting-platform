from django.db import migrations

from platform_core.rls import tenant_rls


class Migration(migrations.Migration):
    dependencies = [
        ("platform_core", "0001_rls_functions"),
        ("audit", "0001_initial"),
    ]

    operations = [
        tenant_rls("audit_event"),
        migrations.RunSQL(
            sql="""
                -- Platform-level events (organisation_id IS NULL), e.g. logins.
                CREATE POLICY platform_event_insert ON audit_event FOR INSERT
                    TO app_rw, app_platform WITH CHECK (organisation_id IS NULL);
                CREATE POLICY platform_event_read ON audit_event FOR SELECT
                    TO app_platform USING (organisation_id IS NULL);
                -- The chaining job works across organisations.
                CREATE POLICY chainer_all ON audit_event TO app_audit
                    USING (true) WITH CHECK (true);

                CREATE FUNCTION audit_event_guard() RETURNS trigger
                    LANGUAGE plpgsql AS $$
                BEGIN
                    IF TG_OP = 'DELETE' THEN
                        RAISE EXCEPTION 'audit_event is append-only'
                            USING ERRCODE = 'insufficient_privilege';
                    END IF;
                    IF OLD.seq IS NOT NULL OR OLD.prev_hash IS NOT NULL OR OLD.hash IS NOT NULL THEN
                        RAISE EXCEPTION 'audit_event % is already chained', OLD.id
                            USING ERRCODE = 'insufficient_privilege';
                    END IF;
                    IF (NEW.id, NEW.organisation_id, NEW.at, NEW.actor_id, NEW.actor_role,
                        NEW.action, NEW.object_type, NEW.object_id, NEW.details)
                       IS DISTINCT FROM
                       (OLD.id, OLD.organisation_id, OLD.at, OLD.actor_id, OLD.actor_role,
                        OLD.action, OLD.object_type, OLD.object_id, OLD.details) THEN
                        RAISE EXCEPTION 'audit_event content is immutable'
                            USING ERRCODE = 'insufficient_privilege';
                    END IF;
                    RETURN NEW;
                END $$;

                CREATE FUNCTION audit_event_no_truncate() RETURNS trigger
                    LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'audit_event cannot be truncated'
                        USING ERRCODE = 'insufficient_privilege';
                END $$;

                CREATE TRIGGER audit_event_guard BEFORE UPDATE OR DELETE ON audit_event
                    FOR EACH ROW EXECUTE FUNCTION audit_event_guard();
                CREATE TRIGGER audit_event_no_truncate BEFORE TRUNCATE ON audit_event
                    FOR EACH STATEMENT EXECUTE FUNCTION audit_event_no_truncate();
            """,
            reverse_sql="""
                DROP TRIGGER audit_event_no_truncate ON audit_event;
                DROP TRIGGER audit_event_guard ON audit_event;
                DROP FUNCTION audit_event_no_truncate();
                DROP FUNCTION audit_event_guard();
                DROP POLICY chainer_all ON audit_event;
                DROP POLICY platform_event_read ON audit_event;
                DROP POLICY platform_event_insert ON audit_event;
            """,
        ),
    ]
