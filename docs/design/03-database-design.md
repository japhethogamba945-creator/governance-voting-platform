# Phase 3 — Database Design

> **Status:** Draft for the Phase 3 gate. The SQL below is a **design specification**. The real schema will be produced by Django migrations in Phase 6 and must match these constraints, triggers and grants. A schema-conformance test checks this.
> Entity purposes, personal-data classification and access rules are in [BLUEPRINT.md §5.1](BLUEPRINT.md#51-entity-catalogue). This document adds the physical design.

## 1. Conventions
- Primary keys are `uuid` (v4), generated in the application with `gen_random_uuid()` as the database fallback. **No sequential IDs anywhere in election tables.**
- Every table that holds an organisation's data has `organisation_id uuid NOT NULL`, plus `UNIQUE (organisation_id, id)` so composite foreign keys can point at it.
- Timestamps are `timestamptz` in UTC. The deliberate exceptions are noted (ballot: none; entitlement: date only).
- Enumerations are `text` columns with `CHECK` constraints, which are easier to migrate than PostgreSQL enum types.
- Soft deletion is used **only** for `member` (`status = 'inactive'`). Election records are never deleted while they are inside their retention period.

## 2. Core DDL (abbreviated)

```sql
-- ---------- tenancy ----------
CREATE TABLE organisation (
  id uuid PRIMARY KEY,
  slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{3,40}$'),
  legal_name text NOT NULL,
  timezone text NOT NULL DEFAULT 'Africa/Nairobi',
  governance_settings jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_user (            -- Django custom user
  id uuid PRIMARY KEY,
  email citext NOT NULL UNIQUE,
  password text NOT NULL,          -- argon2id encoded
  email_verified_at timestamptz,
  status text NOT NULL DEFAULT 'invited' CHECK (status IN ('invited','active','locked','disabled')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE org_membership (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL REFERENCES organisation(id),
  user_id uuid NOT NULL REFERENCES app_user(id),
  UNIQUE (organisation_id, user_id),
  UNIQUE (organisation_id, id)
);

CREATE TABLE role_assignment (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL,
  org_membership_id uuid NOT NULL,
  role text NOT NULL CHECK (role IN ('org_admin','returning_officer','auditor','candidate','member')),
  election_id uuid,                -- NULL = organisation-wide
  granted_by uuid NOT NULL REFERENCES app_user(id),
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz,
  FOREIGN KEY (organisation_id, org_membership_id) REFERENCES org_membership(organisation_id, id)
);

-- ---------- membership ----------
CREATE TABLE member (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL REFERENCES organisation(id),
  member_number text NOT NULL,
  full_name text NOT NULL,
  email citext,
  category text NOT NULL,
  subscription_status text NOT NULL CHECK (subscription_status IN ('paid','unpaid','exempt','n/a')),
  joined_on date NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
  user_id uuid REFERENCES app_user(id),
  UNIQUE (organisation_id, member_number),
  UNIQUE (organisation_id, user_id),
  UNIQUE (organisation_id, id)
);

-- ---------- elections ----------
CREATE TABLE election (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL REFERENCES organisation(id),
  meeting_id uuid,
  title text NOT NULL,
  state text NOT NULL DEFAULT 'draft' CHECK (state IN (
    'draft','nomination','review','ready_for_approval','scheduled','open','closed',
    'counted','awaiting_certification','certified','suspended','disputed','cancelled')),
  seats int NOT NULL CHECK (seats BETWEEN 1 AND 50),
  opens_at timestamptz,
  closes_at timestamptz,
  current_rule_version_id uuid,
  public_key bytea,                -- X25519, set at key ceremony
  public_key_fpr text,
  electorate_digest text,          -- set at open
  ballot_set_digest text,          -- set at close
  CHECK (closes_at IS NULL OR opens_at IS NULL OR closes_at > opens_at),
  CHECK (state IN ('draft','nomination','review','ready_for_approval','cancelled')
         OR (public_key IS NOT NULL AND opens_at IS NOT NULL AND closes_at IS NOT NULL
             AND current_rule_version_id IS NOT NULL)),
  UNIQUE (organisation_id, id)
);

CREATE TABLE election_rule_version (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL,
  election_id uuid NOT NULL,
  version int NOT NULL,
  rules jsonb NOT NULL,            -- validated against a JSON Schema in the application (see §4)
  rules_sha256 text NOT NULL,
  created_by uuid NOT NULL REFERENCES app_user(id),
  approved_by uuid REFERENCES app_user(id),
  approved_at timestamptz,
  UNIQUE (election_id, version),
  CHECK (approved_by IS NULL OR approved_by <> created_by),   -- separation of duties
  FOREIGN KEY (organisation_id, election_id) REFERENCES election(organisation_id, id)
);

CREATE TABLE allowed_transition (  -- reference data, seeded by migration
  from_state text, to_state text, PRIMARY KEY (from_state, to_state)
);

CREATE TABLE lifecycle_transition (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL,
  election_id uuid NOT NULL,
  from_state text NOT NULL, to_state text NOT NULL,
  actor_id uuid REFERENCES app_user(id),   -- NULL = system scheduler
  reason text,
  at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (from_state, to_state) REFERENCES allowed_transition,
  FOREIGN KEY (organisation_id, election_id) REFERENCES election(organisation_id, id)
);

-- ---------- nominations & candidates ----------
CREATE TABLE nomination (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL,
  election_id uuid NOT NULL,
  member_id uuid NOT NULL,
  statement text CHECK (length(statement) <= 4000),
  proposer_member_id uuid, proposer_confirmed_at timestamptz,
  seconder_member_id uuid, seconder_confirmed_at timestamptz,
  status text NOT NULL DEFAULT 'submitted'
    CHECK (status IN ('submitted','withdrawn','approved','rejected')),
  decided_by uuid REFERENCES app_user(id),
  decided_at timestamptz,
  decision_reason text,
  CHECK (status <> 'rejected' OR length(coalesce(decision_reason,'')) > 0),
  CHECK (status NOT IN ('approved','rejected') OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)),
  CHECK (proposer_member_id IS DISTINCT FROM member_id AND seconder_member_id IS DISTINCT FROM member_id),
  UNIQUE (election_id, member_id),
  UNIQUE (organisation_id, id),
  FOREIGN KEY (organisation_id, election_id) REFERENCES election(organisation_id, id),
  FOREIGN KEY (organisation_id, member_id) REFERENCES member(organisation_id, id)
);

CREATE TABLE nomination_document (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL,
  nomination_id uuid NOT NULL,
  object_key text NOT NULL, sha256 text NOT NULL,
  mime text NOT NULL CHECK (mime IN ('application/pdf','image/jpeg','image/png')),
  size_bytes int NOT NULL CHECK (size_bytes <= 5242880),
  scan_status text NOT NULL DEFAULT 'pending' CHECK (scan_status IN ('pending','clean','infected')),
  FOREIGN KEY (organisation_id, nomination_id) REFERENCES nomination(organisation_id, id)
);

CREATE TABLE candidate (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL,
  election_id uuid NOT NULL,
  nomination_id uuid NOT NULL UNIQUE,
  display_name text NOT NULL,
  ballot_order int NOT NULL,
  UNIQUE (election_id, ballot_order),
  UNIQUE (organisation_id, id),
  UNIQUE (election_id, id),        -- target for tally_line FK
  FOREIGN KEY (organisation_id, election_id) REFERENCES election(organisation_id, id),
  FOREIGN KEY (organisation_id, nomination_id) REFERENCES nomination(organisation_id, id)
);

-- ---------- electorate & entitlements ----------
CREATE TABLE electorate_snapshot_entry (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL,
  election_id uuid NOT NULL,
  member_id uuid NOT NULL,
  member_category text NOT NULL,
  eligibility_basis jsonb NOT NULL,     -- which rule clauses were satisfied
  amendment_reason text,                -- non-NULL only if added while suspended
  amended_by uuid REFERENCES app_user(id),
  UNIQUE (election_id, member_id),
  FOREIGN KEY (organisation_id, election_id) REFERENCES election(organisation_id, id),
  FOREIGN KEY (organisation_id, member_id) REFERENCES member(organisation_id, id)
);

CREATE TABLE entitlement (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL,
  election_id uuid NOT NULL,
  member_id uuid NOT NULL,
  status text NOT NULL DEFAULT 'issued' CHECK (status IN ('issued','consumed','revoked','paper')),
  consumed_on date,                      -- deliberately coarse
  idempotency_hash bytea,                -- SHA-256 of the client idempotency key
  revoked_reason text, revoked_by uuid REFERENCES app_user(id),
  UNIQUE (election_id, member_id),       -- ONE entitlement per member per election
  CHECK (status <> 'consumed' OR (idempotency_hash IS NOT NULL AND consumed_on IS NOT NULL)),
  CHECK (status <> 'revoked' OR (revoked_reason IS NOT NULL AND revoked_by IS NOT NULL)),
  FOREIGN KEY (organisation_id, election_id) REFERENCES election(organisation_id, id),
  FOREIGN KEY (organisation_id, member_id) REFERENCES member(organisation_id, id)
);

-- ---------- ballot box (no voter link) ----------
CREATE TABLE ballot (
  id uuid PRIMARY KEY,                   -- random v4, generated by the app
  organisation_id uuid NOT NULL,
  election_id uuid NOT NULL,
  ciphertext bytea NOT NULL CHECK (length(ciphertext) = 4144), -- fixed-size sealed box: 4096 B padded plaintext + 48 B overhead
                                                                    -- (fits 50 selections as candidate UUID strings, see 05 §4)
  receipt_commitment bytea NOT NULL CHECK (length(receipt_commitment) = 32),
  UNIQUE (election_id, receipt_commitment),
  FOREIGN KEY (organisation_id, election_id) REFERENCES election(organisation_id, id)
  -- NO member_id, NO entitlement_id, NO created_at — by design
);

-- ---------- key custody ----------
CREATE TABLE key_share_holder (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL,
  election_id uuid NOT NULL,
  holder_user_id uuid REFERENCES app_user(id),   -- NULL for the sealed offline backup share
  holder_label text NOT NULL,
  share_index smallint NOT NULL,
  bundle_fingerprint text NOT NULL,              -- SHA-256 of the encrypted bundle; the bundle itself is NOT stored
  UNIQUE (election_id, share_index),
  FOREIGN KEY (organisation_id, election_id) REFERENCES election(organisation_id, id)
);

-- ---------- quorum ----------
CREATE TABLE participation_record (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL,
  election_id uuid NOT NULL,
  member_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('registered_attendance','online_ballot','paper')),
  recorded_by uuid REFERENCES app_user(id),
  recorded_on date NOT NULL,
  UNIQUE (election_id, member_id, kind),
  FOREIGN KEY (organisation_id, election_id) REFERENCES election(organisation_id, id),
  FOREIGN KEY (organisation_id, member_id) REFERENCES member(organisation_id, id)
);

-- ---------- tally, recount, certification ----------
CREATE TABLE tally (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL,
  election_id uuid NOT NULL,
  version int NOT NULL,
  kind text NOT NULL CHECK (kind IN ('original','recount')),
  ballot_set_digest text NOT NULL,
  ballots_total int NOT NULL, ballots_valid int NOT NULL, ballots_invalid int NOT NULL,
  entitlements_consumed int NOT NULL,
  reconciliation jsonb NOT NULL,
  reconciliation_ok boolean NOT NULL,
  has_ties boolean NOT NULL,
  run_by uuid NOT NULL REFERENCES app_user(id),
  run_at timestamptz NOT NULL DEFAULT now(),
  reason text,
  UNIQUE (election_id, version),
  CHECK (kind = 'original' OR reason IS NOT NULL),
  CHECK (ballots_total = ballots_valid + ballots_invalid),
  UNIQUE (organisation_id, id),
  FOREIGN KEY (organisation_id, election_id) REFERENCES election(organisation_id, id)
);

CREATE TABLE tally_line (
  tally_id uuid NOT NULL REFERENCES tally(id),
  election_id uuid NOT NULL,
  candidate_id uuid NOT NULL,
  votes int NOT NULL CHECK (votes >= 0),
  rank int NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('elected','not_elected','tied_pending')),
  tie_group smallint,
  PRIMARY KEY (tally_id, candidate_id),
  FOREIGN KEY (election_id, candidate_id) REFERENCES candidate(election_id, id)
);

CREATE TABLE tie_resolution (
  id uuid PRIMARY KEY,
  tally_id uuid NOT NULL REFERENCES tally(id),
  tie_group smallint NOT NULL,
  procedure text NOT NULL,              -- copied from the approved rule version
  outcome jsonb NOT NULL,               -- ordered candidate ids
  evidence text,
  recorded_by uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tally_id, tie_group)
);

CREATE TABLE certification (
  id uuid PRIMARY KEY,
  organisation_id uuid NOT NULL,
  election_id uuid NOT NULL,
  tally_id uuid NOT NULL,
  certified_by uuid NOT NULL REFERENCES app_user(id),
  second_approver uuid REFERENCES app_user(id),
  statement text NOT NULL,
  content_sha256 text NOT NULL,          -- hash of the canonical result document
  certified_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz, published_by uuid REFERENCES app_user(id),
  superseded_at timestamptz, superseded_by uuid REFERENCES certification(id), supersede_reason text,
  CHECK (second_approver IS DISTINCT FROM certified_by),
  FOREIGN KEY (organisation_id, tally_id) REFERENCES tally(organisation_id, id),
  FOREIGN KEY (organisation_id, election_id) REFERENCES election(organisation_id, id)
);
CREATE UNIQUE INDEX one_active_certification ON certification(election_id) WHERE superseded_at IS NULL;

-- ---------- audit ----------
CREATE TABLE audit_event (
  id uuid PRIMARY KEY,
  organisation_id uuid,                  -- NULL = platform-level event
  at timestamptz NOT NULL DEFAULT now(), -- truncated to the minute for 'ballot.accepted'
  actor_id uuid REFERENCES app_user(id),
  actor_role text,
  action text NOT NULL,                  -- e.g. 'election.transition', 'nomination.decided'
  object_type text, object_id uuid,
  details jsonb NOT NULL DEFAULT '{}',   -- validated per action; ballot contents forbidden
  seq bigint,                            -- assigned by the chaining job
  prev_hash bytea, hash bytea,
  UNIQUE (organisation_id, seq)
);

CREATE TABLE audit_anchor (
  id uuid PRIMARY KEY,
  organisation_id uuid,
  up_to_seq bigint NOT NULL,
  head_hash bytea NOT NULL,
  object_key text NOT NULL,              -- location in the object-locked bucket
  anchored_at timestamptz NOT NULL DEFAULT now()
);
```

## 3. Triggers and integrity rules

| Trigger | Table / event | Rule |
|---|---|---|
| `trg_election_transition` | `election` BEFORE UPDATE OF state | `(OLD.state, NEW.state)` must be in `allowed_transition`. The same transaction must insert a matching `lifecycle_transition` row (checked by a deferred constraint trigger). |
| `trg_election_lock` | `election` BEFORE UPDATE | From `scheduled` onward these are immutable: `seats`, `opens_at`, `current_rule_version_id`, `public_key`, `public_key_fpr`. `closes_at` may change **only while** `state = 'suspended'`. `electorate_digest` and `ballot_set_digest` are write-once. |
| `trg_rule_version_lock` | `election_rule_version` BEFORE UPDATE/DELETE | Once `approved_at` is set, the row can't be changed. New versions can't be inserted once the election is `scheduled` or later. |
| `trg_candidate_freeze` | `candidate` INSERT/UPDATE/DELETE | Rejected unless the election is in `review` or `ready_for_approval`. |
| `trg_nomination_freeze` | `nomination` INSERT/UPDATE | New nominations only in `nomination`. Decisions only in `review`. |
| `trg_snapshot_freeze` | `electorate_snapshot_entry` | INSERT allowed while `scheduled` (as part of the opening transaction), or while `suspended` with `amendment_reason` and `amended_by` set. UPDATE and DELETE are always rejected. |
| `trg_entitlement_rules` | `entitlement` | INSERT under the same conditions as the snapshot, and only if a matching snapshot entry exists. The only status changes allowed are `issued→consumed` (election `open`, by `app_voting`) and `issued→revoked` (election `suspended`). Nothing else can change. DELETE is rejected. |
| `trg_ballot_insert` | `ballot` BEFORE INSERT | The election must be `open` with `now()` inside `[opens_at, closes_at)`. This repeats the application's check at the database level. |
| `trg_ballot_immutable` | `ballot` UPDATE/DELETE | Always rejected. The `DELETE` grant is also revoked from every role. Retention destruction uses a separate, audited maintenance procedure run by `app_owner` (§6). |
| `trg_tally_immutable` | `tally`, `tally_line`, `tie_resolution` UPDATE/DELETE | Always rejected. |
| `trg_certification_rules` | `certification` UPDATE | Only these changes are allowed: `published_at`/`published_by` going from NULL to a value, and `superseded_*` going from NULL to a value (and only while the election is `disputed`). |
| `trg_audit_append_only` | `audit_event` UPDATE/DELETE | DELETE is rejected. UPDATE is allowed only for `seq`, `prev_hash` and `hash` going from NULL to a value, and only by role `app_audit` (the chaining job). |
| `trg_tally_preconditions` | `tally` BEFORE INSERT | The election must be `closed`, `awaiting_certification` or `disputed`. `ballot_set_digest` must equal `election.ballot_set_digest`. |

Separate from the roles in [02 §3](02-architecture.md#3-database-roles-and-connection-aliases), an additional role **`app_audit`** has only SELECT and UPDATE(seq, prev_hash, hash) on `audit_event`, plus INSERT on `audit_anchor`.

## 4. Rule-version JSON schema (draft)

```json
{
  "schema": "election-rules/v1",
  "counting_method": "plurality_multi_seat",
  "min_selections": 0,
  "max_selections": "<= seats",
  "partial_ballots_allowed": true,
  "blank_ballot_allowed": true,
  "eligibility": {
    "member_status": ["active"],
    "categories": ["full", "life"],
    "subscription_status": ["paid", "exempt"],
    "joined_before": "2026-11-01T00:00:00+03:00"
  },
  "nomination": {
    "self_nomination": true,
    "proposer_required": false,
    "seconder_required": false,
    "documents_required": false
  },
  "tie_procedure": {"type": "lot_drawn_by_returning_officer", "description": "<text approved before the election>"},
  "quorum": {"basis": "accepted_ballots", "type": "percent_of_electorate", "threshold": 25},
  "early_close_allowed": false,
  "turnout_visible_to_members": false,
  "candidate_order": "alphabetical"
}
```

- The schema is validated in the application.
- `max_selections` resolves to a number ≤ `seats` when the version is approved.
- `joined_before` is optional. If it is omitted there is no tenure cutoff (A6).

## 5. Row-Level Security

Policy applied to every table that carries `organisation_id`, generated by a migration helper:

```sql
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <t> FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON <t>
  USING (organisation_id = nullif(current_setting('app.org_id', true), '')::uuid)
  WITH CHECK (organisation_id = nullif(current_setting('app.org_id', true), '')::uuid);
```

- If no organisation context is set, the expression is `NULL` and **no rows are visible or writable**.
- `app_platform` gets an extra policy only on `organisation` and only for the columns it needs.
- `app_user`, `allowed_transition` and platform-level audit events are not tenant-scoped. Their access is limited by grants instead.

**Tests (continuous integration):**
1. For each tenant table and each application role, the test sets `app.org_id` to Org A and asserts that zero rows from Org B are visible, and that inserting or updating with Org B's ID fails.
2. The same test with no `app.org_id` set expects zero rows.
3. A catalogue query asserts that every table with an `organisation_id` column has RLS enabled and forced. This catches new tables that someone forgot to protect.

## 6. Retention design

All periods are **configurable per organisation**. The values below are defaults to be confirmed under **[LEGAL-REVIEW]**.

| Data | Default retention | How it is disposed of |
|---|---|---|
| Encrypted ballots | Challenge period (configurable, default 90 days after certification) + 12 months | **Crypto-shredding.** Holders are instructed to destroy their key shares, and that is recorded. Ciphertext rows are then deleted by the audited `app_owner` maintenance procedure. |
| Tally, certification, lifecycle, audit events | Permanent (governance record) | Not deleted. Actor personal data can be pseudonymised on a lawful erasure request, without breaking the chain: the hash covers the actor ID, not the name. |
| Electorate snapshot, entitlements, participation records | Same as the encrypted ballots | Deleted together with the ballots. Only aggregate counts are kept in the tally. |
| Nominations and documents | Same as the encrypted ballots; approved candidates' names stay in the results | Object deletion plus the row is deleted. |
| Member register | While active + 24 months after becoming inactive | Hard delete, or pseudonymisation if the member is referenced in a retained record. |
| User accounts | Until deleted by the user or an admin, if no organisation membership remains | Hard delete. Audit events keep the UUID only. |
| Application logs | 90 days | Expire automatically. |
| Backups | 35 days of point-in-time recovery + monthly snapshots kept for 12 months (encrypted) | Expire automatically. A crypto-shredded election stays unreadable in backups because no key shares remain. |

## 7. Indexes (initial)
- `entitlement (election_id, member_id)`: the unique constraint already provides this index.
- `entitlement (election_id, status)`: for counting turnout.
- `ballot (election_id)`: for reading ballots at tally time.
- `audit_event (organisation_id, seq)`; `audit_event (organisation_id, at)`; partial index `audit_event (id) WHERE hash IS NULL`, used by the chaining job.
- `nomination (election_id, status)`, `member (organisation_id, status)`.

## 8. How the schema enforces one vote with secrecy

1. **At most one ballot per voter.** `UNIQUE (election_id, member_id)` on `entitlement` and the `issued→consumed` one-way trigger mean each member can consume a right to vote once. The ballot insert and the consumption share one transaction under row locks. So the number of ballots can't exceed the number of consumed entitlements, which can't exceed the electorate.
2. **No voter link in the ballot.** `ballot` has no column that refers to a member, an entitlement or a time. The only shared key is `election_id`.
3. **Reconciliation.** `tally.entitlements_consumed = tally.ballots_total` is checked on every count. Any difference blocks certification.
