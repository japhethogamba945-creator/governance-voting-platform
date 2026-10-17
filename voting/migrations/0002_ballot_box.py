from django.db import migrations

from platform_core.rls import composite_fk, tenant_rls

SQL = """
-- ---- the ballot box ------------------------------------------------------------
CREATE FUNCTION ballot_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    e record;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'ballots can never be changed or removed'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT state, opens_at, closes_at INTO e FROM election WHERE id = NEW.election_id;
    IF e.state IS DISTINCT FROM 'open' OR now() < e.opens_at OR now() >= e.closes_at THEN
        RAISE EXCEPTION 'voting is not open for this election' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER ballot_guard BEFORE INSERT OR UPDATE OR DELETE ON ballot
    FOR EACH ROW EXECUTE FUNCTION ballot_guard();

CREATE FUNCTION ballot_no_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'the ballot box cannot be truncated' USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER ballot_no_truncate BEFORE TRUNCATE ON ballot
    FOR EACH STATEMENT EXECUTE FUNCTION ballot_no_truncate();

-- Count and order-independent digest of an election's ballots, for sealing the
-- box at close and for reconciliation. Reveals no ballot content; the web role
-- itself has no access to the ballot table.
CREATE FUNCTION ballot_set_summary(p_election uuid)
    RETURNS TABLE (ballot_count bigint, digest text)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT count(*),
           encode(sha256(convert_to(coalesce(string_agg(h, E'\\n' ORDER BY h), ''), 'UTF8')),
                  'hex')
    FROM (SELECT encode(sha256(ciphertext), 'hex') AS h FROM ballot
          WHERE election_id = p_election AND organisation_id = current_org_id()) s
$$;
REVOKE ALL ON FUNCTION ballot_set_summary(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ballot_set_summary(uuid) TO app_rw;

-- ---- stage requirements, now including closing --------------------------------
CREATE OR REPLACE FUNCTION election_stage_requirements() RETURNS trigger LANGUAGE plpgsql AS $$
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
    IF NEW.state = 'closed' AND OLD.state = 'open' THEN
        IF NEW.ballot_set_digest = '' THEN
            RAISE EXCEPTION 'closing requires sealing the ballot box (no ballot-set digest)'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.closes_at > now() THEN
            RAISE EXCEPTION 'the closing time has not been reached'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
"""

REVERSE_SQL = """
DROP FUNCTION ballot_set_summary(uuid);
DROP TRIGGER ballot_no_truncate ON ballot;
DROP FUNCTION ballot_no_truncate();
DROP TRIGGER ballot_guard ON ballot;
DROP FUNCTION ballot_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("voting", "0001_initial"),
        ("electorate", "0003_stage_requirements"),
    ]

    operations = [
        tenant_rls("ballot"),
        composite_fk("ballot", "ballot_election_same_org",
                     ["organisation_id", "election_id"], "election", ["organisation_id", "id"]),
        migrations.RunSQL(sql=SQL, reverse_sql=REVERSE_SQL),
    ]
