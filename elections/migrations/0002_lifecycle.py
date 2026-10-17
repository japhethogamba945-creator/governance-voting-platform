from django.db import migrations

from platform_core.rls import composite_fk, tenant_rls

# (from_state, to_state, reason_required). Must equal elections/lifecycle.py
# TRANSITIONS; tests/test_elections.py checks they stay identical.
TRANSITIONS = [
    ("draft", "nomination", False),
    ("nomination", "review", False),
    ("review", "ready_for_approval", False),
    ("ready_for_approval", "draft", True),
    ("ready_for_approval", "scheduled", False),
    ("scheduled", "open", False),
    ("open", "closed", False),
    ("open", "suspended", True),
    ("suspended", "open", True),
    ("closed", "counted", False),
    ("counted", "awaiting_certification", False),
    ("awaiting_certification", "counted", True),
    ("awaiting_certification", "certified", False),
    ("certified", "disputed", True),
    ("disputed", "counted", True),
    ("disputed", "certified", False),
    ("draft", "cancelled", True),
    ("nomination", "cancelled", True),
    ("review", "cancelled", True),
    ("scheduled", "cancelled", True),
    ("suspended", "cancelled", True),
]

_VALUES = ",\n".join(f"('{a}', '{b}', {str(r).lower()})" for a, b, r in TRANSITIONS)

LIFECYCLE_SQL = f"""
CREATE TABLE allowed_transition (
    from_state text NOT NULL,
    to_state text NOT NULL,
    reason_required boolean NOT NULL DEFAULT false,
    PRIMARY KEY (from_state, to_state)
);
INSERT INTO allowed_transition (from_state, to_state, reason_required) VALUES
{_VALUES};

-- ---- election: creation, transitions and configuration lock -------------
CREATE FUNCTION election_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    editable constant text[] := ARRAY['draft', 'nomination', 'review'];
    t allowed_transition%ROWTYPE;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'draft' THEN
            RAISE EXCEPTION 'a new election starts in draft' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.state = 'cancelled' THEN
        RAISE EXCEPTION 'election % is cancelled and cannot change', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.id, NEW.organisation_id, NEW.created_by_id, NEW.created_at)
       IS DISTINCT FROM (OLD.id, OLD.organisation_id, OLD.created_by_id, OLD.created_at) THEN
        RAISE EXCEPTION 'election identity fields are immutable' USING ERRCODE = 'check_violation';
    END IF;

    -- Configuration may change only while the election is being prepared.
    IF NOT (OLD.state = ANY (editable))
       AND (NEW.title, NEW.meeting_id, NEW.seats, NEW.opens_at, NEW.current_rule_version_id)
           IS DISTINCT FROM
           (OLD.title, OLD.meeting_id, OLD.seats, OLD.opens_at, OLD.current_rule_version_id) THEN
        RAISE EXCEPTION 'election configuration is locked in state %', OLD.state
            USING ERRCODE = 'check_violation';
    END IF;
    -- The closing time may additionally change while suspended (resume with a new time).
    IF NEW.closes_at IS DISTINCT FROM OLD.closes_at
       AND NOT (OLD.state = ANY (editable) OR OLD.state = 'suspended') THEN
        RAISE EXCEPTION 'the closing time can only change while preparing or suspended'
            USING ERRCODE = 'check_violation';
    END IF;
    -- Values set by later stages are write-once.
    IF (OLD.public_key IS NOT NULL AND NEW.public_key IS DISTINCT FROM OLD.public_key)
       OR (OLD.public_key_fpr <> '' AND NEW.public_key_fpr IS DISTINCT FROM OLD.public_key_fpr)
       OR (OLD.electorate_digest <> ''
           AND NEW.electorate_digest IS DISTINCT FROM OLD.electorate_digest)
       OR (OLD.ballot_set_digest <> ''
           AND NEW.ballot_set_digest IS DISTINCT FROM OLD.ballot_set_digest) THEN
        RAISE EXCEPTION 'keys and digests cannot be changed once set'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.current_rule_version_id IS NOT NULL
       AND NEW.current_rule_version_id IS DISTINCT FROM OLD.current_rule_version_id
       AND NOT EXISTS (SELECT 1 FROM election_rule_version v
                       WHERE v.id = NEW.current_rule_version_id AND v.election_id = NEW.id) THEN
        RAISE EXCEPTION 'the rule version belongs to another election'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.state IS DISTINCT FROM OLD.state THEN
        SELECT * INTO t FROM allowed_transition
            WHERE from_state = OLD.state AND to_state = NEW.state;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'transition % -> % is not allowed', OLD.state, NEW.state
                USING ERRCODE = 'check_violation';
        END IF;
        IF t.reason_required
           AND coalesce(btrim(current_setting('app.transition_reason', true)), '') = '' THEN
            RAISE EXCEPTION 'transition % -> % requires a reason', OLD.state, NEW.state
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.state = 'ready_for_approval'
           AND (NEW.current_rule_version_id IS NULL OR NEW.opens_at IS NULL
                OR NEW.closes_at IS NULL) THEN
            RAISE EXCEPTION 'rules and voting times are required before approval'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.state = 'scheduled' THEN
            IF NOT EXISTS (SELECT 1 FROM election_rule_version v
                           WHERE v.id = NEW.current_rule_version_id
                             AND v.election_id = NEW.id AND v.approved_at IS NOT NULL) THEN
                RAISE EXCEPTION 'an approved rule version is required to schedule'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.opens_at <= now() THEN
                RAISE EXCEPTION 'the opening time must be in the future'
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER election_guard BEFORE INSERT OR UPDATE ON election
    FOR EACH ROW EXECUTE FUNCTION election_guard();

CREATE FUNCTION election_no_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'elections are never deleted; cancel instead'
        USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER election_no_delete BEFORE DELETE ON election
    FOR EACH ROW EXECUTE FUNCTION election_no_delete();

-- History is written by the database, not the application, so it cannot be
-- skipped or forged: the application role has no INSERT on this table.
CREATE FUNCTION election_record_transition() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    INSERT INTO lifecycle_transition
        (id, organisation_id, election_id, from_state, to_state, actor_id, reason, at)
    VALUES (gen_random_uuid(), NEW.organisation_id, NEW.id, OLD.state, NEW.state,
            current_app_user_id(),
            coalesce(btrim(current_setting('app.transition_reason', true)), ''), now());
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION election_record_transition() FROM PUBLIC;
CREATE TRIGGER election_record_transition AFTER UPDATE OF state ON election
    FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state)
    EXECUTE FUNCTION election_record_transition();

CREATE FUNCTION lifecycle_transition_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'lifecycle history is append-only' USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER lifecycle_transition_immutable BEFORE UPDATE OR DELETE ON lifecycle_transition
    FOR EACH ROW EXECUTE FUNCTION lifecycle_transition_immutable();

-- ---- rule versions -----------------------------------------------------------
CREATE FUNCTION rule_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    st text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'rule versions are never deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT state INTO st FROM election WHERE id = NEW.election_id;
    IF TG_OP = 'INSERT' THEN
        IF st IS NULL OR st NOT IN ('draft', 'nomination', 'review') THEN
            RAISE EXCEPTION 'rules can only change while the election is being prepared (%)', st
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.approved_at IS NOT NULL OR NEW.approved_by_id IS NOT NULL THEN
            RAISE EXCEPTION 'a new rule version starts unapproved'
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.rules_sha256 := encode(sha256(convert_to(NEW.rules::text, 'UTF8')), 'hex');
        RETURN NEW;
    END IF;
    IF OLD.approved_at IS NOT NULL THEN
        RAISE EXCEPTION 'approved rule versions are immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.id, NEW.organisation_id, NEW.election_id, NEW.version, NEW.rules,
        NEW.rules_sha256, NEW.created_by_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.organisation_id, OLD.election_id, OLD.version, OLD.rules,
        OLD.rules_sha256, OLD.created_by_id, OLD.created_at) THEN
        RAISE EXCEPTION 'rule version content is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF st <> 'ready_for_approval' THEN
        RAISE EXCEPTION 'rules can only be approved while awaiting approval'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER rule_version_guard BEFORE INSERT OR UPDATE OR DELETE ON election_rule_version
    FOR EACH ROW EXECUTE FUNCTION rule_version_guard();
"""

REVERSE_SQL = """
DROP TRIGGER rule_version_guard ON election_rule_version;
DROP FUNCTION rule_version_guard();
DROP TRIGGER lifecycle_transition_immutable ON lifecycle_transition;
DROP FUNCTION lifecycle_transition_immutable();
DROP TRIGGER election_record_transition ON election;
DROP FUNCTION election_record_transition();
DROP TRIGGER election_no_delete ON election;
DROP FUNCTION election_no_delete();
DROP TRIGGER election_guard ON election;
DROP FUNCTION election_guard();
DROP TABLE allowed_transition;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("elections", "0001_initial"),
        ("platform_core", "0001_rls_functions"),
    ]

    operations = [
        tenant_rls("meeting"),
        tenant_rls("election"),
        tenant_rls("election_rule_version"),
        tenant_rls("lifecycle_transition"),
        composite_fk("election", "election_meeting_same_org",
                     ["organisation_id", "meeting_id"], "meeting", ["organisation_id", "id"]),
        composite_fk("election", "election_rule_version_same_org",
                     ["organisation_id", "current_rule_version_id"],
                     "election_rule_version", ["organisation_id", "id"]),
        composite_fk("election_rule_version", "rule_version_election_same_org",
                     ["organisation_id", "election_id"], "election", ["organisation_id", "id"]),
        composite_fk("lifecycle_transition", "lifecycle_transition_election_same_org",
                     ["organisation_id", "election_id"], "election", ["organisation_id", "id"]),
        migrations.RunSQL(sql=LIFECYCLE_SQL, reverse_sql=REVERSE_SQL),
    ]
