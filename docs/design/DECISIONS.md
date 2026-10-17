# Decision Log

The sponsor approved the blueprint (Phase 1 gate) with the instruction: "use your recommended defaults for all decisions". This page records the resulting decisions. Any change to a decision needs a new entry here.

| ID | Decision | Resolution | Consequences / follow-up |
|----|----------|------------|--------------------------|
| D1 | Pilot governing documents | **All governance rules are configuration settings.** Nothing from any constitution is hard-coded. The pilot election is **blocked** at the Phase 8 go/no-go gate until the club's documents have been reviewed and mapped to settings. | Phase 8 includes a mapping of the pilot rules to configuration settings. Development uses fictional rules. |
| D2 | Hybrid / paper voting | **Online only for the MVP.** The schema keeps an entitlement status of `paper` and a participation `kind` of `paper` so paper voting can be added later without a migration. The UI doesn't expose either. | Paper ballots are out of scope until a separate requirements review. |
| D3 | Key custody | **Threshold shares held by people:** 2 of 3 named keyholders (the Returning Officer, the Auditor and an organisation officer), **plus one sealed offline backup share**. Design detail: [05-security-review.md §3](05-security-review.md#3-key-ceremony-and-custody). | Each election needs a key ceremony. The runbook is written in Phase 8. |
| D4 | Hosting region | The blueprint left this open, so this is the resolved default: **any production deployment that holds real personal data will be hosted in a Kenyan data centre, unless a legal review confirms that offshore hosting is allowed.** The stack is kept cloud-portable (containers, PostgreSQL, S3-compatible storage, no proprietary managed services required). Development and staging use only fictional data and may run in any region. | **[LEGAL-REVIEW]** The Data Protection (General) Regulations 2021 contain data-localisation provisions, including one that mentions electoral systems. Whether they apply to private club elections must be checked. The specific provider will be chosen in Phase 8. |
| D5 | Member verification and second factor | Accounts are invited to the email address on the register. Every account needs a **password plus TOTP (an authenticator app)**, with single-use recovery codes. Officials (Org Admin, Returning Officer, Auditor) may also register **WebAuthn passkeys**, and passkeys are encouraged. SMS one-time passcodes are **not** built in the MVP. | There is an adoption risk for members unfamiliar with authenticator apps. It is mitigated by an onboarding window that closes before voting opens, and by an identity-checked reset assisted by the Returning Officer. |
| D6 | Marked-register visibility | **Returning Officer and Auditor:** see each member's voted / not-voted status. **Org Admin:** sees aggregate turnout only. **Members:** see only their own status. | Enforced in permissions and covered by acceptance test 8. |
| D7 | Technology stack | **Python 3.12+, Django 5.2 LTS, PostgreSQL 16+, server-rendered templates with HTMX 2.** | Detail: [02-architecture.md](02-architecture.md). |
| D8 | Repository | **The platform gets its own repository.** Design documents stay here until Phase 6. The new repository is created at the start of Phase 6 (with the sponsor's confirmation of its name and visibility), and these documents move into it. | Implementation code is never added to this repository. |
| A1–A9 | Working assumptions | Accepted as written in the [blueprint, §2.2](BLUEPRINT.md#22-working-assumptions-proceed-unless-corrected-assumption). | — |
| §2.3 | Minor defaults | Accepted as written. | — |
| Ballot design | Separated entitlement + encrypted ballot box with threshold decryption (blueprint §4, option 2) | **Accepted.** End-to-end-verifiable voting stays on the roadmap. | The residual risk that a compromised app server can see choices while voting is open stays on the risk register (threat T6). |

## Phase 2–5 gate (approved by the sponsor: "use your recommended defaults again")

| ID | Decision | Resolution |
|----|----------|------------|
| G5.1 | Phase 2–5 design documents | **Approved** as drafted. |
| G5.2 | Repository (D8) | Name **`governance-voting-platform`**, visibility **private**. The GitHub integration isn't allowed to create repositories, so the sponsor will create an empty repository with this name. Increment 6.1 is committed locally, waiting to be pushed. |
| G5.3 | Residual risks R1–R6 | **Accepted for building the prototype only.** The pilot election is gated again at Phase 8. |
| G5.4 | Design deviation recorded in 6.1 | Recovery codes are a custom hashed device (`accounts.RecoveryCodeDevice`) instead of django-otp's static device, which stores codes in plaintext. This matches 05 §2. |
| G5.5 | Known follow-up | `app_user` has no row-level security yet. Login must look users up before any tenant context exists, so access is limited by grants plus application scoping. To be revisited in 6.2, when organisation admins start creating users. |

## Increment 6.2 decisions

| ID | Decision | Resolution |
|----|----------|------------|
| I6.2.1 | `app_user` row-level security (G5.5) | **Resolved.** A per-request transaction now opens before authentication and sets `app.user_id` from the server-side session. Pre-authentication lookups use `lookup_scope()`, which exposes exactly one row by email (sign-in) or by invitation token hash. |
| I6.2.2 | Membership without an active role | Treated as **no access** (404), so deactivating a member removes their access without deleting history. |
| I6.2.3 | CSV import semantics | Matched on member number, **all or nothing**, never deactivates or deletes, re-validated against the current register at apply time. |
| I6.2.4 | One account per email | Member emails are unique within an organisation. Two members sharing one email address (e.g. a household) cannot both have accounts; one must use a different email. |
| I6.2.5 | Invitation tokens in logs | Tokens are redacted from Django and gunicorn logs (`platform_core/logredact.py`). Reverse proxies or CDNs in front of the app must apply the same redaction, or must not log paths: **deployment checklist item**. |
| I6.2.6 | Officials who are not members | **Resolved in 6.3**: "Invite an official" (see I6.3.4). |

## Increment 6.3 decisions

| ID | Decision | Resolution |
|----|----------|------------|
| I6.3.1 | Who writes the lifecycle history | A `SECURITY DEFINER` trigger writes `lifecycle_transition` whenever `election.state` changes. Actor and reason come from `app.user_id` and `app.transition_reason`. The application role cannot insert, update or delete history rows. |
| I6.3.2 | Separation of duties for rules | Enforced **unconditionally** by a database CHECK: the person who approves a rule version cannot be the person who saved it. A single-person organisation therefore needs a second official (for example an invited independent Returning Officer). |
| I6.3.3 | When configuration locks | Configuration and rules can be edited in draft, nomination and review. They lock at "awaiting approval" so that what the Returning Officer approves is exactly what was submitted. Returning the election to draft (with a reason) unlocks them. |
| I6.3.4 | Officials who are not members | An invitation carries a role instead of a register entry. The role is granted at invitation time and becomes usable once the account is set up. |
| I6.3.5 | Deferred to later increments | Key ceremony at scheduling (6.5); opening with the electorate snapshot (6.5); automatic closing (6.6); counting and certification (6.7); cancelling a suspended election with an administrator's co-signature (two-person rule, later increment). These transitions exist in the state machine but are marked unavailable. |

## Increment 6.4 decisions

| ID | Decision | Resolution |
|----|----------|------------|
| I6.4.1 | Who may be nominated, propose or second | Only members who satisfy the election's eligibility rules (the same rules that will define the electorate). Eligibility is re-checked when a nomination is approved. **[CONFIRM against pilot governing documents]** |
| I6.4.2 | Nominating someone else | The submitter becomes the proposer, and the nominee must consent in the app. Everyone named must confirm before approval. |
| I6.4.3 | When the candidate list freezes | Candidates can change only during review. The ballot order is fixed (alphabetically or by a cryptographic shuffle) at "submit for approval", and the order and its digest are audited. |
| I6.4.4 | Malware scanning of nomination documents | **Not yet implemented.** Documents are marked "not scanned" and the reviewer is told so. Mitigations: the type is checked from the content, files are stored under random names outside any served directory, downloads are attachment-only with a sandboxing CSP, and there are size and count limits. A scanner (for example ClamAV) should be added before the pilot, and documents should be served from a separate download domain (threat T15): **deployment checklist item**. |
| I6.4.5 | Uncontested elections | Allowed (candidates ≤ seats) and flagged in the audit event. Whether candidates are then declared elected without a vote is a governance question for the pilot's rules, not decided by the system. |
| I6.4.6 | Member numbers on the nomination form | Members identify a nominee, proposer or seconder by member number. An error reveals only that no active member has that number. |

## Increment 6.5 decisions

| ID | Decision | Resolution |
|----|----------|------------|
| I6.5.1 | Key custody mechanics (variation of 05 §3) | Instead of downloading passphrase-encrypted bundle files during an attended ceremony, each official **first** creates a keyholder key pair in their own session. Its private key is encrypted under their passphrase (Argon2id). At the ceremony each share is sealed to a keyholder's **public** key, so nobody has to be present and no passphrase is typed at scheduling time. The server stores only sealed shares it cannot open. **Trade-off:** someone who steals the database could attempt offline passphrase guessing against all keyholder keys at once. This is mitigated by a 20-character minimum, Argon2id MODERATE, and needing two keyholders (or one plus the offline backup). A hardware security module or browser-side key generation remains the roadmap answer (residual risk R3). |
| I6.5.2 | Backup share | Encrypted under a random 256-bit code shown **once** on the scheduling confirmation page, which is rendered directly and never stored, cached or audited. It must be printed and sealed offline. Losing it plus one keyholder passphrase makes the election uncountable (R6). |
| I6.5.3 | Memory hygiene | Python cannot guarantee that key material is wiped from memory. It is kept in local variables only and never logged or persisted in plaintext. Accepted as part of R3. |
| I6.5.4 | Who may be a keyholder | Any official (Org Admin, Returning Officer or Auditor) with a keyholder passphrase. The Returning Officer who schedules is always keyholder 1 and picks two others. |
| I6.5.5 | Scheduler access | The scheduler runs as `app_rw`. It lists due elections through policy `scheduler_due` (scheduled or open states only, only while `app.scheduler` is set), then works on each election in its own organisation context. Run **exactly one** scheduler instance. |
| I6.5.6 | Electorate amendments while suspended | Allowed by the database (with a reason) but **not yet exposed** in the UI. It will be added with suspension handling. |

## Increment 6.6 decisions

| ID | Decision | Resolution |
|----|----------|------------|
| I6.6.1 | How casting uses `app_voting` | The ballot insert and the consumption of the entitlement must be one transaction, so casting cannot use a separate connection. `app_rw` is granted membership in `app_voting` **WITH INHERIT FALSE, SET TRUE** and switches with `SET LOCAL ROLE` for the cast only. `app_rw` itself has no privileges on `ballot`; neither role can read ballots. (Variation of 02 §3, which described separate connection aliases.) |
| I6.6.2 | Server sees choices while validating | As designed (threat T6, risk R1). The server must read a ballot to reject over-selection before accepting it. Plaintext exists only in memory during the request and is never logged or stored. End-to-end-verifiable voting remains the roadmap answer. |
| I6.6.3 | Who voted, and when | The entitlement records only the date. The `ballot.accepted` audit event records the voter at minute precision, for the marked register and incident review. Ballots carry no time. Linking ballots to voters would still need physical row order plus two key shares (collusion risk R2). |
| I6.6.4 | Receipt codes without JavaScript | The server generates the code, shows it once and stores only the commitment. The server briefly knows that voter's code: a weaker receipt for no-JavaScript users only. |
| I6.6.5 | Publishing receipt commitments and checking a receipt | Deferred to 6.7 (results). The commitments are stored and the ballot-set digest is fixed at close. |
| I6.6.6 | Early close by the Returning Officer | Not implemented (the rules flag exists). The database requires the closing time to have passed. |
| I6.6.7 | `app_voting` UPDATE (id) on `election` | Needed only so that `SELECT ... FOR SHARE` is permitted. The guard trigger forbids changing an election's id. |
| I6.6.8 | Concurrency test infrastructure | The race tests commit real data, run 20 separate connections and clean up after themselves. The test database is always created, independent of pytest-django's marker detection. |
