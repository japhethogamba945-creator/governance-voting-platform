from django.db import migrations

from platform_core.rls import composite_fk, tenant_rls

GUARDS_SQL = """
CREATE FUNCTION keyholder_key_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'keyholder keys are never deleted; retire instead'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (NEW.id, NEW.organisation_id, NEW.user_id, NEW.public_key, NEW.created_at)
       IS DISTINCT FROM (OLD.id, OLD.organisation_id, OLD.user_id, OLD.public_key, OLD.created_at)
    THEN
        RAISE EXCEPTION 'a keyholder key pair cannot be replaced, only re-protected or retired'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.retired_at IS NOT NULL THEN
        RAISE EXCEPTION 'a retired keyholder key cannot change' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER keyholder_key_guard BEFORE UPDATE OR DELETE ON keyholder_key
    FOR EACH ROW EXECUTE FUNCTION keyholder_key_guard();

-- Shares are written once, during the key ceremony ("Approve and schedule"),
-- and never changed or removed.
CREATE FUNCTION key_share_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    st text;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'key shares are immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT state INTO st FROM election WHERE id = NEW.election_id;
    IF st IS DISTINCT FROM 'ready_for_approval' THEN
        RAISE EXCEPTION 'key shares can only be created by the key ceremony (%)', st
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER key_share_guard BEFORE INSERT OR UPDATE OR DELETE ON key_share
    FOR EACH ROW EXECUTE FUNCTION key_share_guard();
"""

REVERSE_SQL = """
DROP TRIGGER key_share_guard ON key_share;
DROP FUNCTION key_share_guard();
DROP TRIGGER keyholder_key_guard ON keyholder_key;
DROP FUNCTION keyholder_key_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("keys", "0001_initial"),
        ("elections", "0002_lifecycle"),
    ]

    operations = [
        tenant_rls("keyholder_key"),
        tenant_rls("key_share"),
        composite_fk("key_share", "key_share_election_same_org",
                     ["organisation_id", "election_id"], "election", ["organisation_id", "id"]),
        composite_fk("key_share", "key_share_holder_same_org",
                     ["organisation_id", "keyholder_key_id"], "keyholder_key",
                     ["organisation_id", "id"]),
        migrations.RunSQL(sql=GUARDS_SQL, reverse_sql=REVERSE_SQL),
    ]
