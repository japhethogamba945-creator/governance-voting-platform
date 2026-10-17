from django.db import migrations

from platform_core.rls import composite_fk, tenant_rls

GUARDS_SQL = """
-- ---- nominations ---------------------------------------------------------------
CREATE FUNCTION nomination_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    st text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'nominations are never deleted; withdraw instead'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT state INTO st FROM election WHERE id = NEW.election_id;

    IF TG_OP = 'INSERT' THEN
        IF st IS DISTINCT FROM 'nomination' THEN
            RAISE EXCEPTION 'nominations are not open for this election (%)', st
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.status <> 'submitted' OR NEW.decided_by_id IS NOT NULL
           OR NEW.decided_at IS NOT NULL THEN
            RAISE EXCEPTION 'a new nomination starts as submitted and undecided'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF (NEW.id, NEW.organisation_id, NEW.election_id, NEW.nominee_id, NEW.proposer_id,
        NEW.seconder_id, NEW.submitted_by_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.organisation_id, OLD.election_id, OLD.nominee_id, OLD.proposer_id,
        OLD.seconder_id, OLD.submitted_by_id, OLD.created_at) THEN
        RAISE EXCEPTION 'who is nominated, and by whom, cannot change'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'withdrawn' THEN
        RAISE EXCEPTION 'a withdrawn nomination is final' USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NEW.status = 'withdrawn' THEN
            IF OLD.status NOT IN ('submitted', 'approved')
               OR st NOT IN ('nomination', 'review') THEN
                RAISE EXCEPTION 'a nomination can only be withdrawn before the candidate list is fixed'
                    USING ERRCODE = 'check_violation';
            END IF;
        ELSIF st IS DISTINCT FROM 'review' THEN
            RAISE EXCEPTION 'nominations can only be decided while the election is in review (%)', st
                USING ERRCODE = 'check_violation';
        END IF;
    ELSE
        IF (NEW.statement, NEW.nominee_consented_at, NEW.proposer_confirmed_at,
            NEW.seconder_confirmed_at)
           IS DISTINCT FROM
           (OLD.statement, OLD.nominee_consented_at, OLD.proposer_confirmed_at,
            OLD.seconder_confirmed_at)
           AND st IS DISTINCT FROM 'nomination' THEN
            RAISE EXCEPTION 'a nomination can only be edited or confirmed while nominations are open'
                USING ERRCODE = 'check_violation';
        END IF;
        IF (NEW.decided_by_id, NEW.decided_at, NEW.status_reason)
           IS DISTINCT FROM (OLD.decided_by_id, OLD.decided_at, OLD.status_reason)
           AND st IS DISTINCT FROM 'review' THEN
            RAISE EXCEPTION 'decisions can only change while the election is in review'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER nomination_guard BEFORE INSERT OR UPDATE OR DELETE ON nomination
    FOR EACH ROW EXECUTE FUNCTION nomination_guard();

-- ---- candidates: only while the election is in review --------------------------
CREATE FUNCTION candidate_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    st text;
    row_election uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.election_id ELSE NEW.election_id END;
BEGIN
    SELECT state INTO st FROM election WHERE id = row_election;
    IF st IS DISTINCT FROM 'review' THEN
        RAISE EXCEPTION 'the candidate list can only change while the election is in review (%)', st
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM nomination n
                   WHERE n.id = NEW.nomination_id AND n.election_id = NEW.election_id
                     AND n.status = 'approved') THEN
        RAISE EXCEPTION 'a candidate needs an approved nomination for the same election'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER candidate_guard BEFORE INSERT OR UPDATE OR DELETE ON candidate
    FOR EACH ROW EXECUTE FUNCTION candidate_guard();

-- ---- documents: added or removed only while nominations are open -----------------
CREATE FUNCTION nomination_document_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    st text;
    row_nomination uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.nomination_id
                                ELSE NEW.nomination_id END;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (to_jsonb(NEW) - 'scan_status') IS DISTINCT FROM (to_jsonb(OLD) - 'scan_status') THEN
            RAISE EXCEPTION 'only the scan result of a document can change'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    SELECT e.state INTO st FROM nomination n JOIN election e ON e.id = n.election_id
        WHERE n.id = row_nomination;
    IF st IS DISTINCT FROM 'nomination' THEN
        RAISE EXCEPTION 'documents can only change while nominations are open (%)', st
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
CREATE TRIGGER nomination_document_guard BEFORE INSERT OR UPDATE OR DELETE
    ON nomination_document FOR EACH ROW EXECUTE FUNCTION nomination_document_guard();
"""

REVERSE_SQL = """
DROP TRIGGER nomination_document_guard ON nomination_document;
DROP FUNCTION nomination_document_guard();
DROP TRIGGER candidate_guard ON candidate;
DROP FUNCTION candidate_guard();
DROP TRIGGER nomination_guard ON nomination;
DROP FUNCTION nomination_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("nominations", "0001_initial"),
        ("elections", "0002_lifecycle"),
        ("members", "0003_official_invitations"),
    ]

    operations = [
        tenant_rls("nomination"),
        tenant_rls("nomination_document"),
        tenant_rls("candidate"),
        composite_fk("nomination", "nomination_election_same_org",
                     ["organisation_id", "election_id"], "election", ["organisation_id", "id"]),
        composite_fk("nomination", "nomination_nominee_same_org",
                     ["organisation_id", "nominee_id"], "member", ["organisation_id", "id"]),
        composite_fk("nomination", "nomination_proposer_same_org",
                     ["organisation_id", "proposer_id"], "member", ["organisation_id", "id"]),
        composite_fk("nomination", "nomination_seconder_same_org",
                     ["organisation_id", "seconder_id"], "member", ["organisation_id", "id"]),
        composite_fk("nomination", "nomination_submitter_same_org",
                     ["organisation_id", "submitted_by_id"], "member", ["organisation_id", "id"]),
        composite_fk("nomination_document", "nomination_document_same_org",
                     ["organisation_id", "nomination_id"], "nomination",
                     ["organisation_id", "id"]),
        composite_fk("candidate", "candidate_election_same_org",
                     ["organisation_id", "election_id"], "election", ["organisation_id", "id"]),
        composite_fk("candidate", "candidate_nomination_same_org",
                     ["organisation_id", "nomination_id"], "nomination",
                     ["organisation_id", "id"]),
        migrations.RunSQL(sql=GUARDS_SQL, reverse_sql=REVERSE_SQL),
    ]
