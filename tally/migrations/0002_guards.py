from django.db import migrations

from platform_core.rls import composite_fk, tenant_rls

SQL = """
-- ---- tallies: written once, by the counting service as app_tally ------------------
CREATE FUNCTION tally_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    e record;
    box record;
    latest int;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'tallies are immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT state, ballot_set_digest INTO e FROM election WHERE id = NEW.election_id;
    IF e.state IS NULL
       OR e.state NOT IN ('closed', 'counted', 'awaiting_certification', 'disputed') THEN
        RAISE EXCEPTION 'counting is not possible in state %', e.state
            USING ERRCODE = 'check_violation';
    END IF;
    -- The ballot box now must be exactly the box sealed at close, and every
    -- ballot in it must be counted.
    SELECT * INTO box FROM ballot_set_summary(NEW.election_id);
    IF e.ballot_set_digest = '' OR NEW.ballot_set_digest <> e.ballot_set_digest
       OR box.digest <> e.ballot_set_digest THEN
        RAISE EXCEPTION 'the ballot box does not match the digest sealed at close'
            USING ERRCODE = 'check_violation';
    END IF;
    IF box.ballot_count <> NEW.ballots_total THEN
        RAISE EXCEPTION 'a tally must count every ballot in the box'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT coalesce(max(version), 0) INTO latest FROM tally WHERE election_id = NEW.election_id;
    IF NEW.version <> latest + 1 THEN
        RAISE EXCEPTION 'tally versions are consecutive (expected %)', latest + 1
            USING ERRCODE = 'check_violation';
    END IF;
    IF (latest = 0) <> (e.state = 'closed') THEN
        RAISE EXCEPTION 'the original count follows closing; later counts are recounts'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER tally_guard BEFORE INSERT OR UPDATE OR DELETE ON tally
    FOR EACH ROW EXECUTE FUNCTION tally_guard();

CREATE FUNCTION tally_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'tally lines are immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF EXISTS (SELECT 1 FROM tally t WHERE t.election_id = NEW.election_id
                                       AND t.version > (SELECT version FROM tally
                                                        WHERE id = NEW.tally_id))
       OR EXISTS (SELECT 1 FROM tie_resolution r WHERE r.tally_id = NEW.tally_id)
       OR EXISTS (SELECT 1 FROM certification c WHERE c.tally_id = NEW.tally_id) THEN
        RAISE EXCEPTION 'lines can only be added to the latest, unresolved tally'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER tally_line_guard BEFORE INSERT OR UPDATE OR DELETE ON tally_line
    FOR EACH ROW EXECUTE FUNCTION tally_line_guard();

-- ---- tie resolutions -------------------------------------------------------------------
CREATE FUNCTION tie_resolution_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    t record;
    e record;
    n_elected int;
    n_tied int;
    n_named int;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'tie resolutions are immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT * INTO t FROM tally WHERE id = NEW.tally_id;
    SELECT el.state, el.seats, v.rules->'tie_procedure'->>'type' AS procedure INTO e
        FROM election el JOIN election_rule_version v ON v.id = el.current_rule_version_id
        WHERE el.id = t.election_id;
    IF e.state IS DISTINCT FROM 'counted' OR NOT t.has_ties
       OR EXISTS (SELECT 1 FROM tally x WHERE x.election_id = t.election_id
                                          AND x.version > t.version) THEN
        RAISE EXCEPTION 'a tie can only be resolved on the latest count, while counted'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.procedure IS DISTINCT FROM e.procedure THEN
        RAISE EXCEPTION 'the tie procedure must be the one in the approved rules'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT count(*) FILTER (WHERE outcome = 'elected'),
           count(*) FILTER (WHERE outcome = 'tied_pending' AND tie_group = NEW.tie_group)
        INTO n_elected, n_tied FROM tally_line WHERE tally_id = NEW.tally_id;
    SELECT count(DISTINCT l.candidate_id) INTO n_named
        FROM jsonb_array_elements_text(NEW.elected || NEW.not_elected) AS x(id)
        JOIN tally_line l ON l.tally_id = NEW.tally_id AND l.candidate_id = x.id::uuid
                         AND l.outcome = 'tied_pending' AND l.tie_group = NEW.tie_group;
    IF n_tied = 0 OR jsonb_array_length(NEW.elected) <> e.seats - n_elected
       OR jsonb_array_length(NEW.elected) + jsonb_array_length(NEW.not_elected) <> n_tied
       OR n_named <> n_tied THEN
        RAISE EXCEPTION 'the outcome must place every tied candidate and fill exactly the '
                        'seats in the tie' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER tie_resolution_guard BEFORE INSERT OR UPDATE OR DELETE ON tie_resolution
    FOR EACH ROW EXECUTE FUNCTION tie_resolution_guard();

-- ---- certification ---------------------------------------------------------------------
CREATE FUNCTION certification_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    t record;
    st text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'certifications are never deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT state INTO st FROM election WHERE id = NEW.election_id;
    IF TG_OP = 'INSERT' THEN
        SELECT * INTO t FROM tally WHERE id = NEW.tally_id;
        IF st IS DISTINCT FROM 'awaiting_certification' THEN
            RAISE EXCEPTION 'only an election awaiting certification can be certified'
                USING ERRCODE = 'check_violation';
        END IF;
        IF t.election_id IS DISTINCT FROM NEW.election_id
           OR EXISTS (SELECT 1 FROM tally x WHERE x.election_id = t.election_id
                                              AND x.version > t.version) THEN
            RAISE EXCEPTION 'only the latest count can be certified'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NOT t.reconciliation_ok THEN
            RAISE EXCEPTION 'a count that failed reconciliation cannot be certified'
                USING ERRCODE = 'check_violation';
        END IF;
        IF t.has_ties AND NOT EXISTS (SELECT 1 FROM tie_resolution r WHERE r.tally_id = t.id) THEN
            RAISE EXCEPTION 'resolve the tie before certifying' USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM election el
                   JOIN election_rule_version v ON v.id = el.current_rule_version_id
                   WHERE el.id = NEW.election_id AND v.created_by_id = NEW.certified_by_id) THEN
            RAISE EXCEPTION 'the person who wrote the rules cannot certify the result'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.published_at IS NOT NULL OR NEW.superseded_at IS NOT NULL THEN
            RAISE EXCEPTION 'a new certification starts unpublished'
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.content_sha256 := encode(sha256(convert_to(NEW.document, 'UTF8')), 'hex');
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.organisation_id, NEW.election_id, NEW.tally_id, NEW.certified_by_id,
        NEW.statement, NEW.document, NEW.content_sha256, NEW.certified_at)
       IS DISTINCT FROM
       (OLD.id, OLD.organisation_id, OLD.election_id, OLD.tally_id, OLD.certified_by_id,
        OLD.statement, OLD.document, OLD.content_sha256, OLD.certified_at) THEN
        RAISE EXCEPTION 'a certified result cannot be changed' USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.published_at, NEW.published_by_id) IS DISTINCT FROM
       (OLD.published_at, OLD.published_by_id) THEN
        IF OLD.published_at IS NOT NULL OR OLD.superseded_at IS NOT NULL
           OR st IS DISTINCT FROM 'certified' THEN
            RAISE EXCEPTION 'only a current certified result can be published, once'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF (NEW.superseded_at, NEW.supersede_reason) IS DISTINCT FROM
       (OLD.superseded_at, OLD.supersede_reason) THEN
        IF OLD.superseded_at IS NOT NULL OR st IS DISTINCT FROM 'disputed' THEN
            RAISE EXCEPTION 'a certified result can only be superseded, once, while disputed'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER certification_guard BEFORE INSERT OR UPDATE OR DELETE ON certification
    FOR EACH ROW EXECUTE FUNCTION certification_guard();

-- ---- what each result stage requires of the election -------------------------------
CREATE FUNCTION election_result_requirements() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    t record;
BEGIN
    SELECT * INTO t FROM tally WHERE election_id = NEW.id ORDER BY version DESC LIMIT 1;
    IF NEW.state = 'counted' THEN
        IF t.id IS NULL THEN
            RAISE EXCEPTION 'counted requires a tally' USING ERRCODE = 'check_violation';
        END IF;
        IF OLD.state = 'disputed'
           AND EXISTS (SELECT 1 FROM certification c WHERE c.tally_id = t.id) THEN
            RAISE EXCEPTION 'a disputed result goes back to counted only with a recount'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.state = 'awaiting_certification' THEN
        IF t.id IS NULL OR NOT t.reconciliation_ok
           OR (t.has_ties AND NOT EXISTS (SELECT 1 FROM tie_resolution r
                                          WHERE r.tally_id = t.id)) THEN
            RAISE EXCEPTION 'certification needs a reconciled count with any tie resolved'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.state = 'certified' THEN
        IF NOT EXISTS (SELECT 1 FROM certification c
                       WHERE c.election_id = NEW.id AND c.tally_id = t.id
                         AND c.superseded_at IS NULL) THEN
            RAISE EXCEPTION 'certified requires a current certification of the latest count'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER election_result_requirements BEFORE UPDATE OF state ON election
    FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state
                       AND NEW.state IN ('counted', 'awaiting_certification', 'certified'))
    EXECUTE FUNCTION election_result_requirements();

-- ---- staged key contributions expire (DECISIONS I6.7.1) ------------------------------
-- The scheduler deletes expired contributions across organisations, only while
-- it has set app.scheduler (platform_core.db.scheduler_scope).
CREATE POLICY scheduler_expired_select ON key_contribution FOR SELECT TO app_rw USING (
    current_setting('app.scheduler', true) = 'on' AND expires_at < now());
CREATE POLICY scheduler_expired_delete ON key_contribution FOR DELETE TO app_rw USING (
    current_setting('app.scheduler', true) = 'on' AND expires_at < now());

-- ---- the counting role and the published receipt list --------------------------------
GRANT EXECUTE ON FUNCTION ballot_set_summary(uuid) TO app_tally;

-- Sorted receipt commitments of a sealed ballot box, for members to check their
-- receipt. Commitments reveal nothing about choices; sorting hides storage order.
CREATE FUNCTION ballot_receipt_commitments(p_election uuid) RETURNS SETOF text
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT encode(b.receipt_commitment, 'hex') AS c FROM ballot b
    JOIN election e ON e.id = b.election_id
    WHERE b.election_id = p_election AND b.organisation_id = current_org_id()
      AND e.organisation_id = current_org_id() AND e.ballot_set_digest <> ''
    ORDER BY 1
$$;
REVOKE ALL ON FUNCTION ballot_receipt_commitments(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ballot_receipt_commitments(uuid) TO app_rw;
"""

REVERSE_SQL = """
DROP FUNCTION ballot_receipt_commitments(uuid);
REVOKE EXECUTE ON FUNCTION ballot_set_summary(uuid) FROM app_tally;
DROP POLICY scheduler_expired_delete ON key_contribution;
DROP POLICY scheduler_expired_select ON key_contribution;
DROP TRIGGER election_result_requirements ON election;
DROP FUNCTION election_result_requirements();
DROP TRIGGER certification_guard ON certification;
DROP FUNCTION certification_guard();
DROP TRIGGER tie_resolution_guard ON tie_resolution;
DROP FUNCTION tie_resolution_guard();
DROP TRIGGER tally_line_guard ON tally_line;
DROP FUNCTION tally_line_guard();
DROP TRIGGER tally_guard ON tally;
DROP FUNCTION tally_guard();
"""

_ORG = ["organisation_id"]


class Migration(migrations.Migration):
    dependencies = [
        ("tally", "0001_initial"),
        ("voting", "0002_ballot_box"),
        ("keys", "0003_keyshare_recipient_public_key"),
    ]

    operations = [
        *[tenant_rls(t) for t in ("count_request", "key_contribution", "tally", "tally_line",
                                  "tie_resolution", "certification")],
        composite_fk("count_request", "count_request_election_same_org",
                     [*_ORG, "election_id"], "election", ["organisation_id", "id"]),
        composite_fk("count_request", "count_request_tally_same_election",
                     ["election_id", "tally_id"], "tally", ["election_id", "id"]),
        composite_fk("key_contribution", "key_contribution_request_same_org",
                     [*_ORG, "count_request_id"], "count_request", ["organisation_id", "id"]),
        composite_fk("tally", "tally_election_same_org",
                     [*_ORG, "election_id"], "election", ["organisation_id", "id"]),
        composite_fk("tally_line", "tally_line_tally_same_org",
                     [*_ORG, "tally_id"], "tally", ["organisation_id", "id"]),
        composite_fk("tally_line", "tally_line_tally_same_election",
                     ["election_id", "tally_id"], "tally", ["election_id", "id"]),
        composite_fk("tally_line", "tally_line_candidate_same_election",
                     ["election_id", "candidate_id"], "candidate", ["election_id", "id"]),
        composite_fk("tie_resolution", "tie_resolution_tally_same_org",
                     [*_ORG, "tally_id"], "tally", ["organisation_id", "id"]),
        composite_fk("certification", "certification_election_same_org",
                     [*_ORG, "election_id"], "election", ["organisation_id", "id"]),
        composite_fk("certification", "certification_tally_same_election",
                     ["election_id", "tally_id"], "tally", ["election_id", "id"]),
        migrations.RunSQL(sql=SQL, reverse_sql=REVERSE_SQL),
    ]
