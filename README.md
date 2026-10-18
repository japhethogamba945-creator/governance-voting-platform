# Governance Voting Platform

A multi-organisation voting and governance platform for clubs and membership organisations: membership registers, nominations, secret-ballot remote elections, counting, certification and audit.

> **Status: prototype, increments 6.1–6.8 (foundation; membership and invitations; meetings, elections and lifecycle; nominations; keys and electorate; secret ballot; counting and results; audit chain, quorum and auditor views). Phase 6 is complete; Phase 7 (verification) is next.** Not for real elections. Development uses fictional data only. The design and approval history live in [`docs/design/`](docs/design/README.md).

## Increment 6.8: audit chain and anchoring, quorum, auditor views

| Area | Implemented |
|---|---|
| Hash chain | Each organisation's audit events, plus one platform-level chain, are numbered and linked: `hash = SHA-256(prev_hash ‖ canonical JSON)`, with RFC 8785 (JCS) canonical form for every value type written. The chaining runs in `run_scheduler` as `app_audit`, the only role that can set the chain fields, under an advisory lock. The web role is never granted `app_audit` |
| Anchors | When a chain has new events, the chain head is recorded every 15 minutes, and at once when voting opens or closes, a count completes, a result is certified or a result is published. Each anchor goes in an append-only table **and** a write-once, read-only file under `AUDIT_ANCHOR_DIR` (production: an object-lock location; deployment checklist). Auditors are emailed the chain head when voting opens or closes and when a result is certified. Each certified result document now includes the chain head at certification |
| Verification | `manage.py verify_audit --org <slug>`, the "Verify the audit chain" button, and `verify_audit --bundle <file>` on a downloaded offline bundle all recompute the chain. They also check every anchor against the chain and against its file. Edited, removed or reordered events are detected, and so is a chain rewritten with recomputed hashes and a forged anchor row (the anchor file disagrees) |
| Quorum | The approved rule (none / absolute / percent of electorate, measured by accepted ballots or registered attendance) is assessed with exact arithmetic (percentages rounded up) and recorded in the certified result as **met or not met**. The system reports it and does not decide validity (A8), so the 6.7 block on certification is removed |
| Attendance register | When the basis is registered attendance, the Returning Officer records attending members by member number (step-up). The database accepts only members of the frozen electorate, from opening until certification |
| Auditor views | Audit trail with filters, verification and anchors. The Org Admin's view leaves out who voted (D6), and the offline bundle (which includes it) is for the Returning Officer and Auditor only. "Election checks" recomputes every fingerprint: rules, counting key, electorate, ballot box, ballots equal voters, each count's reconciliation, and each certified document's SHA-256, ballot box and receipt list |
| Acceptance tests | **8** (administrative views and audit records do not disclose individual choices: no candidate appears in any voting or counting record, the administrator sees no totals before publication and no marked register), plus chain tamper detection and quorum. The 6.7 acceptance-test numbering is corrected above |
| Tests | 335 |

## Increment 6.7: counting, ties, recounts, certification and results

| Area | Implemented |
|---|---|
| Counting with two keyholders | The Returning Officer starts the count (step-up). Two **different people** then each provide a share in their own session: a keyholder passphrase, or the sealed backup code. The first share is opened and the one piece it shares with each other holder is sealed to that holder's public key, so nothing stored is enough to rebuild the key. Staged pieces expire after 30 minutes and are deleted by the scheduler. The second share plus the piece staged for it rebuild the key **in memory**, and the count runs at once (I6.7.1) |
| The count | Runs as `app_tally` (`SET LOCAL ROLE`), the only role that can read ciphertexts, and it cannot read receipt commitments. Every ballot is decrypted and re-validated: wrong key, wrong election, unknown or duplicate candidates, or too many choices make it **invalid**. Only totals are kept. Individual ballots are never stored, logged or audited |
| Reconciliation | The ballot box matches the digest sealed at close; ballots equal the members marked as having voted; every ballot is valid; voters are within the electorate. The database refuses a tally whose digest or ballot count differs from the box. A failed check leaves the election in "Counted" and blocks certification |
| Seats and ties | Plurality, multi-seat. A tie for the last seat(s) is **never broken by the system**: those candidates stay "tied" until the Returning Officer records the outcome of the procedure from the approved rules, with evidence. The database checks that the outcome fills exactly the seats in the tie and uses the approved procedure |
| Recounts | Ordered with a reason from Counted, Awaiting certification or Disputed. Each recount is a new, immutable tally version, and earlier versions are kept. A recount of a disputed result withdraws (supersedes) its certification |
| Certification | The Returning Officer certifies (step-up) a canonical JSON result document. The database stores it byte for byte with its SHA-256 and refuses certification by the person who wrote the rules, or of anything but the latest reconciled count with any tie resolved. Rules with a quorum cannot be certified until 6.8 |
| Publication | A separate step (Org Admin or Returning Officer, step-up). Members see results only after publication. They can view the document and download it; its SHA-256 equals the published fingerprint. "Mark disputed" and "Re-certify" are available |
| Receipt check | After close, members see the sorted list of receipt commitments (via a `SECURITY DEFINER` function) and its digest. The check runs **only in the browser**: the code is hashed locally and never sent to the server |
| Immutability | Tallies, lines and tie resolutions cannot be changed or deleted. A certification can only be published once, or superseded once while disputed. Even the superuser is stopped by the triggers |
| Acceptance tests | 9 (the tally matches an independent reference: 3,000 random allocations and a 200-ballot reference count), 10 (partial and blank ballots counted correctly), 11 (ties flagged and resolved only by the approved procedure), 13 (recounts keep the original tally and record the new one), 14 (certified results cannot be changed, even by the database owner). Also: no results before certification and publication; a reconciliation failure blocks certification. *(Numbering corrected in 6.8.)* |
| Verified in a browser | Headless Chromium at phone width, results and receipt check: the correct code is found and highlighted, a wrong code is not, and no request is made during the check |
| Tests | 308 |

## Increment 6.6: secret ballot casting and closing

| Area | Implemented |
|---|---|
| Ballot pages | Mobile-first ballot → review → cast → receipt. The review step carries the choices in the form itself (they are never stored between steps). A live "n of max" counter disables extra choices; the server enforces every rule regardless (maximum, minimum, partial and blank ballots, duplicates, unknown or foreign candidates). Works without JavaScript |
| Secrecy | The server validates the ballot, then seals it with libsodium `crypto_box_seal` to the election's public key. The plaintext is bound to the election id and padded to 4096 bytes, so every ciphertext is 4144 bytes. The `ballot` row has **no** voter reference and **no** timestamp. Choices never reach logs, audit events, email or sessions (tested with a canary candidate) |
| Separation of roles | Casting switches to `app_voting` (`SET LOCAL ROLE`) inside the casting transaction only. `app_voting` may insert ballots but not read them; `app_rw` has no access to the ballot table at all. Even the table owner cannot update, delete or truncate ballots |
| One person, one vote | One transaction: lock the election `FOR SHARE` (closing takes `FOR UPDATE`, so it waits for casts in progress), lock the entitlement `FOR UPDATE`, validate, seal, insert, consume. A signed idempotency key, bound to the voter's entitlement, makes a retry of the same form return "accepted" truthfully; any other attempt gets "already accepted". The success page is sent only after the transaction commits |
| Receipts | An 80-bit code generated **in the browser**; only its SHA-256 commitment reaches the server and is stored with the ballot. Without JavaScript, the server generates the code and shows it once. The receipt shows no choices |
| Closing | The scheduler closes elections at the closing time and seals the ballot box: the ballot count and an order-independent digest are computed by a narrow `SECURITY DEFINER` function (no content exposed), stored write-once, audited and reconciled against consumed entitlements |
| Marked register | The Returning Officer and Auditor see who has voted (never how); administrators see turnout only (D6) |
| Acceptance tests | 3 (ineligible voters via direct requests), **4 (200 concurrent duplicate submissions on 20 connections → exactly one ballot)**, 5 (outside the window, including after the closing time before the scheduler runs), 6 (over-selection), 7 (receipt), 8 (no choices in admin views, audit, logs, email or sessions), 15 (failure before commit records nothing; a retry after a lost response is answered truthfully) |
| Verified in a browser | Headless Chromium at phone width: selection limit, review, submission via JavaScript, receipt code generated in the browser and matching the stored commitment, code absent from server logs |
| Tests | 267 |

## Increment 6.5: key ceremony, electorate and opening

| Area | Implemented |
|---|---|
| Keyholders | Officials set a keyholder passphrase (at least 20 characters, step-up). This creates a personal X25519 key pair whose private half is stored only encrypted under the passphrase (Argon2id, libsodium MODERATE, ~0.5 s and ~256 MiB per unlock). Changing the passphrase keeps the key pair |
| Key ceremony | Part of "Approve and schedule": the Returning Officer picks two other keyholders. An election X25519 key is generated and split by 2-of-4 replicated secret sharing (three keyholders plus a sealed offline backup). Every pair is self-tested before anything is stored. Each share is sealed to its keyholder's public key; the backup share is encrypted under a one-time 256-bit backup code that is shown exactly once and never stored. The public key and its fingerprint are recorded and audited |
| Key reconstruction | `keys.services.rebuild_election_key` rebuilds the private key from any two holders (passphrase or backup code), for counting in 6.7. Tested for all six pairs, wrong passphrases, non-keyholders and single shares |
| Opening | `manage.py run_scheduler` (cron or a timer every minute, or `--loop`) opens scheduled elections at their opening time, each in its own transaction and organisation context. It lists due elections through a narrow policy that exposes only scheduled and open elections, only while the scheduler scope is set. An election with no eligible voters stays scheduled and the failure is audited |
| Electorate and entitlements | At opening the electorate is frozen: every active member who meets the approved eligibility rules gets an electorate entry and exactly one entitlement (`UNIQUE (election, member)`). A fingerprint of the electorate is stored on the election (write-once) and audited |
| Database enforcement | Scheduling requires the ceremony (public key plus at least three shares); opening requires the frozen electorate and the opening time to have passed. Shares are written only during the ceremony and never change. Electorate entries are added only at opening (or, with a reason, while suspended) and are never changed or deleted. An entitlement changes once, from issued: to consumed only while voting is open, or to revoked only while suspended. Nothing is deleted |
| Tests | 237 |

## Increment 6.4: candidate nominations

| Area | Implemented |
|---|---|
| Eligibility | One function (`elections/eligibility.py`) applies an election's eligibility rules (active status, category, subscription, optional membership cut-off) with plain-language reasons. It will also build the electorate snapshot in 6.5 |
| Submitting | Members self-nominate (if the rules allow) or nominate another member, becoming their proposer. Proposer and seconder are required when the rules say so. Nominee, proposer and seconder must be different eligible members. One live nomination per member per election; a withdrawn one may be resubmitted |
| Confirming | Everyone named who did not submit the nomination is emailed and must confirm in the app; anyone named may decline (which withdraws it). The nominee or submitter may withdraw until the candidate list is fixed |
| Documents | Up to 3 PDF/JPEG/PNG files of at most 5 MB. The type comes from the file's content, never the browser's claim. Stored privately under random names (never served directly) with SHA-256; downloadable only by officials and the nominee/submitter, as an attachment with a sandboxing CSP |
| Review | The Returning Officer approves or rejects (with a reason) during review, with step-up. Approval re-checks confirmations, required documents and current eligibility. An approval can be reversed during review. Approved nominations become candidates and receive the election-scoped Candidate role |
| Candidate list | Submitting for approval requires every nomination decided and at least one candidate, then fixes the ballot order (alphabetical, or random using a cryptographic generator) and audits the order with a digest |
| Database enforcement | Nominations can only be submitted, edited or confirmed while nominations are open and decided only in review; who is nominated and by whom never changes; withdrawn is final; nothing is deleted. Candidates change only during review and need an approved nomination of the same election. Documents change only while nominations are open |
| Tests | 217, including the cross-tenant matrix run by a user holding every role so each route's real lookup is exercised |

## Increment 6.3: meetings, elections, rules and lifecycle

| Area | Implemented |
|---|---|
| Meetings | Create, edit and list meetings (AGM, SGM, other); elections can be attached to a meeting |
| Elections | Create and edit drafts (title, seats, voting window in the organisation's time zone, minimum one hour of voting) |
| Rules | A validated rules document (selection limits, partial/blank ballots, eligibility categories, subscription statuses, optional membership cut-off, nomination requirements, tie-break procedure and its approved wording, quorum, early close, candidate order). Each save is a new numbered version; the database computes its SHA-256 fingerprint |
| Lifecycle | The full state machine (draft → nomination → review → awaiting approval → scheduled → open → closed → counted → awaiting certification → certified, plus suspended, disputed and cancelled). Who may perform each transition, which need a reason and which need step-up are defined in `elections/lifecycle.py`. Transitions whose preconditions belong to later increments (opening, closing, counting, certification) exist but are not yet available |
| Database enforcement (acceptance test 12) | Triggers allow only the defined transitions, require reasons where marked, lock seats, times, title and rules once an election is submitted for approval, allow the closing time to change only while suspended, keep later keys and digests write-once, forbid deleting elections or rule versions, and make cancelled final. Approving the rules requires someone other than their author. The transition history is written **by the database itself** (the app cannot insert, edit or delete it) |
| Officials | "Invite an official" grants Returning Officer, Auditor or Administrator roles to people who are not members, using the same single-use invitation flow |
| Tests | 177 |

## Increment 6.2: organisation, membership and invitations

| Area | Implemented |
|---|---|
| Membership register | Add, edit, search/filter, deactivate (step-up + mandatory reason) and reactivate members. Unique member number and email per organisation. Eligibility-relevant changes (category, subscription, joined date, status) are audited with old and new values; other personal fields by name only |
| CSV import | Upload → preview (create / update / unchanged / error per row) → apply with step-up. All or nothing; never deactivates; refused if the register changed since the preview |
| Invitations | Single-use, 7-day token (only its SHA-256 is stored), resend revokes the previous link, token moved out of the URL immediately and redacted from all logs; acceptance sets a password and goes straight into authenticator enrolment. Inviting an email that already has an account links it without revealing that to the administrator |
| Roles | People-and-roles page; org admins grant/revoke Returning Officer, Auditor and Org Admin (step-up, audited); cannot change own roles; the last administrator cannot be removed (locked against concurrent revocation). The Member role follows the register |
| Access | A membership with no active role now has no access (same 404 as a non-member) |
| Row-level security on `app_user` | Users are visible only to themselves, to organisations they belong to, and transiently to a sign-in lookup by exact email or an invitation lookup by token. Resolves decision-log item G5.5 |
| Tests | 129, including an extended cross-tenant matrix that also checks every object URL against another organisation's object IDs |

## What increment 6.1 contains

| Area | Implemented |
|---|---|
| Project skeleton | Django 5.2 LTS, PostgreSQL 16, server-rendered templates, no inline scripts (strict CSP) |
| Database roles | `db/roles.sql`; deny-by-default grants manifest (`platform_core/grants.py`) re-applied after every migration |
| Tenant isolation | `organisation_id` on every tenant table, forced Row-Level Security, composite foreign keys, `TenantManager` app-level scoping, organisation resolved from URL + membership (404 for non-members) |
| Authentication | Email + password (Argon2id) + mandatory TOTP; hashed single-use recovery codes; account and IP throttling with no account enumeration; session rotation, idle/absolute timeouts (stricter for officials); step-up decorator for privileged actions |
| RBAC | Role → permission table (`tenancy/roles.py`), `@requires(Perm...)` view decorator |
| Audit (minimal) | Append-only `audit_event` (trigger blocks update/delete/truncate; only the future chaining role may fill hash fields once); ballot-like keys rejected from details |
| Platform Owner | `provision_organisation` command on a separate `app_platform` connection |
| Tests | 58 tests against real PostgreSQL running as `app_rw`: RLS catalogue guard, grants-manifest guard, DB-level and HTTP-level cross-tenant matrix (auto-discovers every `/o/<org_slug>/` route), auth, permissions, headers, audit immutability |

Next increments are listed in [`docs/design/BLUEPRINT.md` §9](docs/design/BLUEPRINT.md#9-phased-implementation-plan-with-acceptance-gates).

## Local development

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL 16 (or Docker).

```sh
# 1. PostgreSQL: either `docker compose up db`, or on a local cluster:
psql -U postgres -c "CREATE ROLE gvp_admin LOGIN SUPERUSER PASSWORD 'gvp_admin'"   # dev/test only
psql -U postgres -c "CREATE DATABASE gvp OWNER gvp_admin"
psql -U postgres -d gvp -f db/roles.sql

# 2. Python
uv sync
cp .env.example .env && set -a && . ./.env && set +a

# 3. Migrate as the admin login (no assumed role), then run as app_rw
DB_ASSUME_ROLE= uv run python manage.py migrate
uv run python manage.py provision_organisation --slug acacia-club \
    --name "Acacia Gardens Club (fictional)" --admin-email chair@acacia.example
uv run python manage.py dev_activate_user chair@acacia.example 'a long dev passphrase'
uv run python manage.py runserver
```

Sign in at <http://localhost:8000/accounts/login/>; first sign-in enrols an authenticator app.

### Tests

```sh
uv run pytest            # needs the gvp_admin login and db/roles.sql applied
uv run ruff check .
```

The test runner migrates as the admin login, then switches the connection to `app_rw` so the whole suite runs under production privileges and RLS (`tests/conftest.py`).

## Production notes

- Each process logs in directly as its own role (`app_rw`, `app_platform`, ...); never as an administrator, and without `DB_ASSUME_ROLE`.
- Migrations run as `app_owner` in a separate release step.
- Hosting: a Kenyan data centre by default for real personal data (decision D4, pending legal review).
- See `docs/design/05-security-review.md` before any deployment.
