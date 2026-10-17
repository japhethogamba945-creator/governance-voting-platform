from django.db import migrations

from platform_core.rls import composite_fk, tenant_rls

GUARDS_SQL = """
-- The electorate is frozen when voting opens. Rows are added only while the
-- election is 'scheduled' (the opening transaction) or, with a recorded reason,
-- while it is suspended. Nothing is ever updated or deleted.
CREATE FUNCTION electorate_entry_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    st text;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'the electorate is frozen' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT state INTO st FROM election WHERE id = NEW.election_id;
    IF NOT (st = 'scheduled' OR (st = 'suspended' AND NEW.amendment_reason <> '')) THEN
        RAISE EXCEPTION 'the electorate can only be set when voting opens (%)', st
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER electorate_entry_guard BEFORE INSERT OR UPDATE OR DELETE ON electorate_entry
    FOR EACH ROW EXECUTE FUNCTION electorate_entry_guard();

CREATE FUNCTION entitlement_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    st text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'entitlements are never deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT state INTO st FROM election WHERE id = NEW.election_id;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'issued' THEN
            RAISE EXCEPTION 'a new entitlement starts as issued' USING ERRCODE = 'check_violation';
        END IF;
        IF st NOT IN ('scheduled', 'suspended') THEN
            RAISE EXCEPTION 'entitlements are issued only when voting opens (%)', st
                USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM electorate_entry e
                       WHERE e.election_id = NEW.election_id AND e.member_id = NEW.member_id) THEN
            RAISE EXCEPTION 'an entitlement needs a matching electorate entry'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.organisation_id, NEW.election_id, NEW.member_id)
       IS DISTINCT FROM (OLD.id, OLD.organisation_id, OLD.election_id, OLD.member_id) THEN
        RAISE EXCEPTION 'entitlement identity is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status <> 'issued' OR NEW.status = OLD.status THEN
        RAISE EXCEPTION 'an entitlement can only change once, from issued'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status = 'consumed' AND st <> 'open' THEN
        RAISE EXCEPTION 'a ballot can only be cast while voting is open (%)', st
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status = 'revoked' AND st <> 'suspended' THEN
        RAISE EXCEPTION 'an entitlement can only be revoked while voting is suspended (%)', st
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status NOT IN ('consumed', 'revoked') THEN
        RAISE EXCEPTION 'invalid entitlement change % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER entitlement_guard BEFORE INSERT OR UPDATE OR DELETE ON entitlement
    FOR EACH ROW EXECUTE FUNCTION entitlement_guard();
"""

REVERSE_SQL = """
DROP TRIGGER entitlement_guard ON entitlement;
DROP FUNCTION entitlement_guard();
DROP TRIGGER electorate_entry_guard ON electorate_entry;
DROP FUNCTION electorate_entry_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("electorate", "0001_initial"),
        ("elections", "0002_lifecycle"),
        ("members", "0003_official_invitations"),
    ]

    operations = [
        tenant_rls("electorate_entry"),
        tenant_rls("entitlement"),
        composite_fk("electorate_entry", "electorate_entry_election_same_org",
                     ["organisation_id", "election_id"], "election", ["organisation_id", "id"]),
        composite_fk("electorate_entry", "electorate_entry_member_same_org",
                     ["organisation_id", "member_id"], "member", ["organisation_id", "id"]),
        composite_fk("entitlement", "entitlement_election_same_org",
                     ["organisation_id", "election_id"], "election", ["organisation_id", "id"]),
        composite_fk("entitlement", "entitlement_member_same_org",
                     ["organisation_id", "member_id"], "member", ["organisation_id", "id"]),
        migrations.RunSQL(sql=GUARDS_SQL, reverse_sql=REVERSE_SQL),
    ]
