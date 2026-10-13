# Phase 5 — Security Review (Pre-implementation)

> **Status:** Draft for the Phase 5 gate. This is the design-level review that the blueprint requires before any code is written.
> It is **not** a substitute for the independent penetration test and cryptography review scheduled for Phase 8. Nothing here claims absolute anonymity, legal compliance or certification.

## 1. Scope and method
- **Covered:** authentication, session management, tenant isolation, ballot secrecy, duplicate prevention, key custody, audit integrity, logging, privacy, backup and recovery, incident response, and the secure development lifecycle.
- **Method:** a walkthrough of the threats (blueprint [§6](BLUEPRINT.md#6-threat-model-main-election-integrity-risks)) against the Phase 2–4 designs, a check against **OWASP ASVS 4.0 Level 2**, and a list of residual risks with owners.

## 2. Authentication and session design

| Control | Design |
|---|---|
| Password storage | Argon2id through Django's `Argon2PasswordHasher`. Parameters at least as strong as OWASP's recommendation (memory ≥ 64 MiB, time cost ≥ 3, parallelism 1, tuned so one hash takes about 250 ms on production hardware). Hashes are upgraded automatically at login when the parameters change. |
| Password policy | At least 12 characters, no composition rules, no forced rotation. Rejected if found in the Have I Been Pwned breached-password list (checked with the k-anonymity API, so the password never leaves the server) or in a local list of the 100,000 most common passwords. |
| Second factor | TOTP (RFC 6238, 30-second step, ±1 step tolerance, a code can be used only once). 10 single-use recovery codes, stored hashed. WebAuthn passkeys for officials, encouraged. A second factor is **mandatory** for every account (D5). |
| Step-up authentication | Asks for the second factor again if the last check was more than 5 minutes ago, before: approving rules, the key ceremony, suspend/resume/cancel, starting a tally, recording a tie resolution, recounts, certification, publication, role grants, second-factor resets, and CSV imports. |
| Rate limiting | Stored in PostgreSQL (no Redis): per account 5 failures → exponentially increasing lockout (1, 2, 4… min, capped at 1 hour); per IP 50 login attempts per 10 minutes; edge/WAF limits per IP. Separate limits for password reset, invitation resend and ballot POSTs (10 per minute per user). |
| Account enumeration | Identical responses and similar response times for "unknown email" and "wrong password". Password reset always replies "If an account exists, we've sent an email". Invitation acceptance never reveals which other organisations an email belongs to. |
| Invitation / reset tokens | 256-bit random, only a SHA-256 hash is stored, single use. Invitations expire after 7 days, reset links after 30 minutes. Using one ends all other sessions. |
| Lost second factor | Reset by the Org Admin **and** the Returning Officer (two people), each with step-up. The reason must record how identity was checked. The member is emailed. **While an election the member can vote in is open**, the reset also requires the Auditor to be notified and is highlighted in the audit trail. |
| Sessions | Stored on the server (database backend). Cookies are `__Host-` prefixed, `Secure`, `HttpOnly`, `SameSite=Lax`. A new session ID is issued at login and at privilege change. Idle timeout 30 minutes (members) or 15 minutes (officials); absolute limit 12 hours. A "sign out everywhere" option. The list of active sessions is visible to the user. |
| Web protections | Django CSRF protection on all state-changing requests (HTMX sends the token in a header). Strict CSP with nonces and no `unsafe-inline`. HSTS with preload. `X-Content-Type-Options`. `frame-ancestors 'none'`. Templates escape output automatically. Uploaded documents are served from a separate domain as attachments. |

## 3. Key ceremony and custody

**Model (D3):** 2-of-4 **replicated secret sharing**. The four holders are the Returning Officer, the Auditor, an organisation officer and a sealed offline backup. Any two holders can rebuild the key, and one holder alone learns nothing.

**Why replicated sharing rather than Shamir:** for small groups (n ≤ 5, k = 2 or 3) it needs only random bytes and XOR. That means no polynomial arithmetic or rarely used library to get wrong, and it is easy for a reviewer to check.

**Construction:**
- Let `sk` be the 32-byte X25519 private key.
- For each pair of holders {i, j} (6 pairs for 4 holders), draw a fresh random 32-byte `r_ij`.
- Holder *i* receives `(pair_id, r_ij)` and holder *j* receives `(pair_id, sk ⊕ r_ij)`.
- Each holder's bundle has 3 pieces. Each piece on its own is uniformly random and independent of the others, so one bundle reveals nothing about `sk`.
- Any two holders combine their pieces for the pair they share.

**Bundle protection:**
- Each bundle is serialised and encrypted with `crypto_secretbox`. The encryption key comes from the holder's passphrase via `crypto_pwhash` (Argon2id, `OPSLIMIT_SENSITIVE` / `MEMLIMIT_MODERATE`).
- Passphrase: at least 5 words or 20 characters, with a generated passphrase suggested.
- The bundle file contains its own fingerprint and the election's public-key fingerprint.

**Ceremony procedure (summary; the full runbook is written in Phase 8):**
1. The Returning Officer starts it from the approval screen (step-up). The Auditor attends as a witness.
2. The tally worker generates `sk` and `pk` in memory that can't be swapped to disk (`mlock`), builds the bundles, and records the public key, its fingerprint and the bundle fingerprints.
3. Each holder, in their own authenticated session, sets a passphrase and downloads their bundle. They then **prove they can decrypt it** by entering the passphrase again, which the worker checks.
4. The backup bundle's passphrase is generated, printed with the bundle as QR code plus text, sealed in a tamper-evident envelope signed across the seal, and stored offline by the organisation.
5. **Self-test:** a test message is encrypted to `pk` and decrypted using the key rebuilt from *every* pair. All 6 must succeed.
6. `sk` and all pieces are wiped from memory, and an audit event records the fingerprints. The plaintext shares are never written to the database or to disk.

**Accepted limitation:** while the ceremony is running, the server holds `sk` and sees the passphrases. Ceremonies are short, attended, logged and audited. Generating the key in a browser, or in a hardware security module, is on the roadmap.

## 4. Ballot encoding and cryptography
- **Plaintext:** a 2-byte big-endian length, followed by canonical JSON `{"v":1,"e":"<election_id>","s":[<sorted candidate UUIDs>]}`, padded with zero bytes to **4,096 bytes**.
- **Encryption:** `crypto_box_seal` (X25519 + XSalsa20-Poly1305), giving a fixed 4,144-byte ciphertext.
- **Election binding:** the election ID inside the plaintext stops a ballot from one election being counted in another.
- **At tally:** a ballot that doesn't decrypt, names a different election, lists unknown or duplicate candidates, or exceeds `max_selections` is counted as **invalid**. Because the server has already validated every ballot before accepting it, **any invalid ballot means tampering or a bug**. The tally flags it as a discrepancy and certification is blocked.
- **Ballot-set digest:** SHA-256 over the *sorted* SHA-256 hashes of the ciphertexts. Sorting makes it independent of the order ballots were stored in, so it doesn't leak insertion order. It is computed and stored when voting closes, and recomputed by every tally and recount.
- **Receipt:**
  - `receipt_secret` is 80 random bits, shown to the voter as a 16-character code in Crockford base32.
  - The ballot stores `receipt_commitment = SHA-256("rcpt/v1" ‖ election_id ‖ receipt_secret)`.
  - After the election closes, the sorted list of commitments is published to members.
  - Checking a code hashes it the same way and looks it up. Guessing a valid code would take on the order of 2⁸⁰ tries.

## 5. Duplicate prevention — verification plan

The design is in [BLUEPRINT §4.2](BLUEPRINT.md#42-recommended-design-option-2-recommendation) and enforced by the schema in [03 §8](03-database-design.md#8-how-the-schema-enforces-one-vote-with-secrecy). Required tests:

| Test | Method | Pass condition |
|---|---|---|
| Parallel duplicates | 200 parallel processes submit for the same entitlement with different keys | Exactly 1 ballot; 199 × 409 |
| Retry with the same key | Same key resent 50 times, before and after commit | 1 ballot; every reply is "accepted" after commit, and "not accepted" if the first attempt failed |
| Interrupted after commit | A fault proxy drops the response after COMMIT | The retry returns "accepted"; 1 ballot |
| Interrupted before commit | A fault is injected before COMMIT (connection killed) | 0 ballots; entitlement still `issued`; the retry succeeds once |
| Closing race | 100 submissions fired across the `closes_at` boundary | Every accepted ballot's transaction started before `closes_at` (database time); none accepted after the close transition commits; consumed entitlements = ballots |
| Bypassing the app at the database | Direct INSERT into `ballot` as `app_rw`; UPDATE of a consumed entitlement | Both rejected (grant and trigger) |

## 6. Audit integrity
- **Writing:** every privileged action writes an `audit_event` in the **same transaction** as the change. The `details` field is checked against a schema for each action, and fields that could carry ballot contents are forbidden.
- **Chaining:** the `app_audit` job runs every 5 seconds. It assigns `seq` per organisation in insertion order and sets:
  - `hash = SHA-256(prev_hash ‖ JCS(event without hash))`, where JCS is the RFC 8785 canonical JSON format.

  Only one chaining job runs at a time (advisory lock).
- **Anchoring:**
  - Every 15 minutes, and immediately at open, close, tally and certification, the chain head of each organisation is written to an object-lock bucket in **compliance mode** (retention 7 years, configurable).
  - The head hash is embedded in the certified result document.
  - At open, close and certification the head hash is also emailed to the Auditor.
- **Verification:** `manage.py verify_audit --org <slug>` and the offline verification bundle recompute the chain and compare it with the anchors. The Auditor runs this before certification. Running it is part of the certification checklist.
- **Limitation:** a database superuser could rewrite events added since the last anchor (at most 15 minutes, or the time since the last key event). The anchors make anything older detectable.

## 7. Logging and data-leak policy
- **Never log:** request bodies, ballot selections, receipt secrets, idempotency keys, passwords, TOTP secrets or codes, key shares, passphrases, session IDs.
- Logs use an allow-list of fields per route. Error reports scrub local variables and request data on the voting, keys and accounts modules.
- There is **no third-party analytics or tracking** on any authenticated page.
- Emails and notifications never contain ballot contents or live turnout figures.
- The canary-candidate test ([02 §8](02-architecture.md#8-observability-without-leaking-ballots)) runs in continuous integration and again in staging before the pilot.

## 8. Privacy (DPIA outline) [LEGAL-REVIEW]
The DPIA draft will cover the following. The structure follows good practice; the legal tests must be checked against the Act, its Regulations and ODPC guidance.
1. **Description of the processing:** membership administration, electorate determination, authentication, nominations, voting, audit.
2. **Roles:**
   - the organisation is the controller;
   - the platform operator is the processor;
   - sub-processors are the hosting provider, the email provider and the error tracker. They are listed, and their locations recorded.
3. **Necessity and proportionality:**
   - data minimisation (no national ID numbers in the MVP, phone number optional);
   - purpose limitation (election data used only for running and auditing elections);
   - retention schedule as in [03 §6](03-database-design.md#6-retention-design).
4. **Risks to people and mitigations:** someone's vote revealed (§3, §4), the voter list leaking (RLS, access control), account takeover (§2), wrongful exclusion from the electorate (approval preview, amendment process).
5. **Data subject rights:**
   - access and correction for register data;
   - erasure limited where the organisation must keep governance records;
   - ballots hold no personal data, so these rights can't be exercised against them, by design.
6. **Transfers and localisation:** Kenyan hosting for production (D4); check whether the sub-processors' locations are allowed.
7. **Breach handling:** see §10.

## 9. Backup, recovery and availability
- **Targets (production):** RPO ≤ 5 minutes (≈ 0 for accepted ballots, see below); RTO ≤ 4 hours.
- **Synchronous replication while elections are open:** the primary sets `synchronous_standby_names` with one standby during any open election. A ballot reported as accepted is then never lost in a failover. (Asynchronous replication would allow a receipt to exist with no ballot behind it.)
- **Backups:** continuous WAL archiving with point-in-time recovery for 35 days, plus encrypted daily base backups and monthly snapshots kept for 12 months. Backups are stored in a second facility, still within the approved jurisdiction.
- **Restoring during an open election:** a restore can roll back accepted ballots. The procedure is:
  1. Suspend the election.
  2. Restore to the latest consistent point.
  3. Compare the restored ballot commitments with the receipts that voters report.
  4. The Returning Officer decides the remedy, recorded in the audit trail. Possible remedies are: extend the window, re-issue the affected entitlements, or cancel.

  This is an exceptional, documented procedure. It is never automatic.
- **Restore drills:** a full restore into an isolated environment is run before the pilot (acceptance test 16) and every quarter after that. Each drill checks the audit chain, the counts and a test tally of a fictional election.
- **Availability:** at least 2 web instances, health checks, a load test at 2× the expected peak, and a runbook for "Suspend and extend" in case of a voting-day outage.

## 10. Incident response (summary)
- **Severities:** S1 (secrecy or integrity of an election possibly compromised), S2 (outage during voting), S3 (other).
- **For S1/S2:**
  - the on-call engineer tells the organisation's Returning Officer within 1 hour;
  - the RO decides whether to suspend;
  - evidence is preserved (logs, database snapshot, audit anchors).
- **Personal data breaches:** the controller (the organisation) is notified without delay so it can meet its notification duties to the ODPC and affected people. **[LEGAL-REVIEW]**: confirm current deadlines and content requirements (the Act sets a short statutory window).
- **Afterwards:** a post-incident review is written and logged in the audit trail where it relates to an election.

## 11. Secure development lifecycle
- Protected `main` branch: every change goes through a pull request, needs 1 review (2 for the `voting/`, `keys/`, `tally/` and `audit/` modules and for migrations), and continuous integration must pass.
- Dependencies are pinned with hashes (`uv`/`pip-tools`). Dependabot is enabled. `pip-audit` fails the build on known-vulnerable packages.
- Static analysis (bandit, semgrep with Django rules) and secret scanning run in continuous integration. Container images are scanned with Trivy.
- Migrations that touch triggers, grants or RLS require the schema-conformance test plus a second reviewer.
- Production deployments use signed container images and keep an audit log of who deployed what.

## 12. Manual security tests (Phase 7)
1. Insecure direct object references: swap `org_slug`, `election_id`, `nomination_id` and `member_id` between organisations and between roles. Expect 404 everywhere.
2. Tamper with the ballot form: extra candidate IDs, candidate IDs from another election, duplicates, more than `max`, an empty ballot when blanks aren't allowed, a replayed or forged idempotency key.
3. Check for session fixation and session reuse after logout, and CSRF on every POST.
4. Check timing and messages for account enumeration on login, reset and invitation.
5. Bypass step-up by calling a privileged endpoint directly with a stale session.
6. Try to change rules, candidates or the electorate after scheduling, through the UI, the API and the database (as each role).
7. Attempt to read `ballot` and the key shares from the `app_rw` and `app_platform` roles.
8. Uploads: polyglot PDF, oversized file, wrong MIME type, EICAR test file.
9. Search logs, the error tracker and email for leaks (canary).
10. Tamper with the audit chain: change an old event directly in the database and confirm verification detects it.
11. Run the restore drill and check counts and digests.
12. Test on mobile browsers: Chrome on Android (low-end device), Safari on iOS, Samsung Internet.

## 13. Residual risk register (pilot)

| # | Risk | Likelihood | Impact | Owner | Treatment |
|---|---|---|---|---|---|
| R1 | A compromised app server sees choices while voting is open (T6) | Low | High | Platform | Accept for the pilot; harden; end-to-end-verifiable voting on the roadmap |
| R2 | A database administrator and *k* keyholders collude to break secrecy | Low | High | Org + Platform | Accept; keyholders are appointed by the organisation; audited access |
| R3 | The key ceremony exposes `sk` on the server | Low | High | Platform | Accept for the pilot; browser or HSM key generation later |
| R4 | Coercion of remote voters (someone standing over them) | Medium | Medium | Organisation | Inherent to remote voting; disclose to members |
| R5 | Members struggle with TOTP, which lowers turnout | Medium | Medium | Organisation | Onboarding window, help guide, assisted reset |
| R6 | Losing more than 2 key bundles makes the count impossible | Low | High | Organisation | Backup share; runbook; reminder before close |
| R7 | It is unclear whether remote e-voting is lawful under the pilot club's constitution and the applicable law | Unknown | High | Organisation | **Blocks the pilot** until reviewed (D1) |
| R8 | Data-localisation requirements | Unknown | Medium | Platform | Kenyan hosting by default (D4); legal review |
| R9 | A denial-of-service attack during voting | Medium | Medium | Platform | WAF, capacity, suspend-and-extend procedure |

## 14. Gate checklist (Phase 5 → Phase 6)
- [ ] Sponsor approves Phases 2–5 (this document, together with 02, 03 and 04)
- [ ] Sponsor confirms the name and visibility of the new repository (D8)
- [ ] Residual risks R1–R6 accepted for building the **prototype** (the pilot itself is gated again in Phase 8)
