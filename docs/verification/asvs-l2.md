# OWASP ASVS 4.0.3 Level 2: self-assessment

> **This is a self-assessment by the development team, not an independent verification.** It records, chapter by chapter, how the prototype meets the Application Security Verification Standard (ASVS) 4.0.3 Level 2 requirements that apply to it, with the evidence. An independent penetration test and cryptography review are scheduled for Phase 8 (BLUEPRINT §9). Nothing here claims compliance or certification.

**Status key:** **Met** (implemented, with automated evidence) · **Partial** (implemented in part; the gap is listed) · **Gap** (not implemented; tracked in the findings below) · **Deployment** (depends on the hosting set-up, checked at Phase 8) · **N/A** (the feature does not exist in this application).

## Summary

| Chapter | Status | Main evidence | Open items |
|---|---|---|---|
| V1 Architecture | Met / Deployment | `docs/design/02`, `03`, `05`; threat model in 05 | Deployment architecture review (Phase 8) |
| V2 Authentication | Partial | `tests/test_auth.py`, `tests/test_hardening.py` | F-3 (no self-service recovery); F-2 fixed (needs the mirror at deployment) |
| V3 Sessions | Met | `tests/test_auth.py`, `tests/test_permissions_and_headers.py` | — |
| V4 Access control | Met | `tests/test_tenant_isolation_http.py`, `test_tenant_isolation_db.py`, `test_rls_catalogue.py`, `test_grants.py` | — |
| V5 Validation, encoding | Met | forms, `tests/test_voting.py`, `tests/test_import.py` | — |
| V6 Cryptography | Partial | `keys/crypto.py`, `tests/test_keys_electorate.py`, `tests/test_tally.py` | R3 (server-side key generation), independent crypto review (Phase 8) |
| V7 Errors and logging | Partial | `platform_core/logredact.py`, `tests/test_log_redaction.py`, audit chain | Central log store and alerting (Deployment) |
| V8 Data protection | Met / Deployment | no-store, field-level minimisation, `tests/test_voting.py` canary | Backup encryption (Deployment) |
| V9 Communications | Deployment | HSTS, secure cookies, SSL redirect settings | TLS configuration at the edge |
| V10 Malicious code | Met | `uv.lock`, `pip-audit`, bandit in CI | — |
| V11 Business logic | Met | lifecycle state machine and database triggers; stress tests; ballot rate limit (`tests/test_hardening.py`) | — (F-1 fixed) |
| V12 Files | Partial | `nominations` upload checks and tests | F-4 (malware scanning, I6.4.4) |
| V13 API | N/A / Met | no public API; CSRF on every POST | — |
| V14 Configuration | Met / Deployment | strict CSP and headers, `tests/test_permissions_and_headers.py` | Container and host hardening (Deployment) |

## Findings to resolve before the pilot

| ID | ASVS | Finding | Proposed treatment |
|---|---|---|---|
| F-1 | 11.1.2, 11.1.4 | The design's per-user rate limit on ballot POSTs (10 per minute, 05 §2) was not implemented. | **Fixed:** at most `BALLOT_POSTS_PER_MINUTE` (10) review and cast submissions per member per minute; HTTP 429 with `Retry-After`. A throttled cast by a member whose ballot was already accepted is answered "already accepted", never "not recorded" (acceptance test 15). Tests: `tests/test_hardening.py`. |
| F-2 | 2.1.7 | New passwords were checked against Django's common-password list and a 12-character minimum, but not against a breached-password corpus. | **Fixed in code:** `BreachedPasswordValidator` checks an offline mirror in the k-anonymity range format; the password never leaves the server. **Deployment:** provide the mirror (`BREACHED_PASSWORDS_DIR`, [procedure](../operations/breached-passwords.md)); `check --deploy` warns without it. |
| F-3 | 2.5.x | There is no self-service password reset. Recovery is by recovery code (second factor) or by an administrator re-inviting the member. Safe, but support-heavy (residual risk R5). | Decide with the pilot organisation; if needed, add reset links that follow 2.5 (single use, short-lived, no account enumeration). |
| F-4 | 12.4.2 | Uploaded nomination documents are not scanned for malware (I6.4.4). Mitigated by content-type checks, size limits, random storage names and attachment-only downloads with a sandboxing CSP. | Add a scanner (e.g. ClamAV) and a separate download domain (deployment checklist). |
| F-5 | 1.x | Anchor storage, the job-process login, backups and TLS depend on the hosting set-up (I6.8.3, I6.8.7, 05 §9). | Verify on the real infrastructure at Phase 8. |

## Chapter detail

### V1 Architecture, design and threat modelling
- 1.1 Secure development lifecycle: design phases with recorded approvals (`docs/design/`), threat model (05), decision log, pull requests with CI. **Met.**
- 1.2 Authentication architecture: one authentication path, second factor mandatory for every account (D5). **Met.**
- 1.4 Access control architecture: deny-by-default permission matrix (`tenancy/roles.py`), organisation resolved from the URL and membership, enforced again by PostgreSQL row-level security and composite foreign keys. **Met.**
- 1.5 Input and output architecture: Django forms server-side; templates auto-escape; strict CSP. **Met.**
- 1.6 Cryptographic architecture: libsodium (PyNaCl) only; key custody documented (05 §3, I6.5.1, I6.7.1). **Met**; independent review pending.
- 1.7 Logging architecture: audit events in the same transaction as each change, hash chain and anchors (05 §6); application logs with redaction. **Met**; central log store is **Deployment**.
- 1.8 Data protection: classification in BLUEPRINT §5; retention in 03 §6. **Met.**
- 1.14 Configuration architecture: all secrets from the environment; separate least-privilege database roles. **Met / Deployment.**

### V2 Authentication
- 2.1 Passwords: minimum 12 characters, no maximum below 64, Unicode allowed, no composition rules, Argon2id storage (`accounts/hashers.py`). Breached-password check against an offline mirror (`accounts/validators.py`, `test_breached_passwords_are_refused`): **Met** once the mirror is deployed (F-2).
- 2.2 General authenticator security: lockout with exponential back-off per account and per IP, identical responses for unknown and wrong (`test_account_lockout_after_five_failures`, `test_lockout_applies_equally_to_unknown_emails`, `test_wrong_password_and_unknown_email_look_identical`). **Met.**
- 2.4 Credential storage: Argon2id; throttle keys are HMACs, never raw emails (`test_throttle_keys_do_not_store_email`). **Met.**
- 2.5 Credential recovery: recovery codes single use and hashed (`test_recovery_code_is_single_use_and_stored_hashed`); no self-service reset: **Partial (F-3)**.
- 2.7/2.8 One-time passwords: TOTP RFC 6238, ±1 step, codes cannot be replayed (`test_totp_code_cannot_be_replayed`), enrolment with recovery codes shown once. **Met.**
- 2.9 Cryptographic authenticators (WebAuthn): not implemented; optional in the design. **N/A** for this prototype.

### V3 Session management
- 3.1–3.3 Server-side sessions; key rotated at login (`test_session_key_rotates_on_login`); logout by POST only and invalidates the session (`test_logout_requires_post`); idle timeout 30 minutes and stricter for officials (`test_idle_timeout_logs_out`, `test_official_idle_timeout_is_stricter`); absolute timeout 12 hours. **Met.**
- 3.4 Cookies: `__Host-` prefix, Secure, HttpOnly, SameSite=Lax for session and CSRF cookies. **Met.**
- 3.7 Re-authentication for sensitive actions: step-up second factor within 5 minutes before privileged actions (`test_step_up_redirects_when_mfa_is_stale`). **Met.**

### V4 Access control
- 4.1 Enforced on the server, deny by default, least privilege: permission checks on every view (`requires`), the route matrix tests every organisation URL for cross-tenant and object-level access (`tests/test_tenant_isolation_http.py`). **Met.**
- 4.2 Operation-level and object-level: RLS forced on every tenant table (`test_every_tenant_table_has_forced_rls_and_tenant_policy`); privileges equal the manifest exactly (`test_table_privileges_match_manifest_exactly`); separation of duties in the database (rule author cannot approve or certify). **Met.**
- 4.3 Administrative interfaces: the Django admin is not installed; platform operations use a separate role (`app_platform`). **Met.**

### V5 Validation, sanitisation and encoding
- 5.1 Input validation: Django forms with explicit fields; ballot validation repeated in the casting service and again at count (`test_invalid_selections_are_rejected_and_nothing_is_recorded`, `test_count_matches_a_reference_tally_and_flags_invalid_ballots`); CSV import validated row by row, all or nothing (`tests/test_import.py`). **Met.**
- 5.2/5.3 Sanitisation and output encoding: template auto-escaping; no `|safe` on user data; CSV export not offered. **Met.**
- 5.3.4 SQL injection: ORM or parameterised SQL everywhere; bandit B608 clean in CI. **Met.**
- 5.5 Deserialisation: JSON only (`json.loads` of data the application wrote). **Met.**

### V6 Stored cryptography
- 6.2 Algorithms: X25519 sealed boxes for ballots, XSalsa20-Poly1305 for key material, Argon2id for keyholder passphrases, SHA-256 for digests and the audit chain. No custom primitives. **Met.**
- 6.3 Random values: `secrets` and libsodium CSPRNG. **Met.**
- 6.4 Secret management: election private key exists only in memory during the key ceremony and the count; shares are sealed to keyholders; backup code shown once (I6.5.2). Server-side key generation is residual risk R3. **Partial** pending the independent cryptography review.

### V7 Error handling and logging
- 7.1 Log content: no request bodies, choices, passwords, codes, tokens or passphrases (`tests/test_log_redaction.py`, canary tests in `test_voting.py`, `test_tally.py`). **Met.**
- 7.2 Security events: authentication, access-control decisions on privileged actions and all election actions are audit events. **Met.**
- 7.3 Log protection: audit events append-only in the database, hash-chained and anchored (`tests/test_audit_chain.py`). **Met**; shipping application logs to a protected central store is **Deployment**.
- 7.4 Error handling: generic error pages; no stack traces with `DEBUG` off (the default). **Met.**

### V8 Data protection
- 8.1/8.2 Authenticated pages are `Cache-Control: no-store` (`test_authenticated_pages_are_not_cached`); the backup code page is rendered once and never stored. **Met.**
- 8.3 Sensitive private data: ballots carry no voter reference and no time; who voted is visible only to the Returning Officer and Auditor (D6); invitation tokens are redacted from logs. **Met.**
- Backups encrypted and kept in the approved jurisdiction: **Deployment** (05 §9).

### V9 Communications
- HSTS (2 years, preload), SSL redirect, secure cookies and `SECURE_PROXY_SSL_HEADER` behind a trusted proxy are configured. TLS versions, ciphers and certificate management are at the edge: **Deployment**.

### V10 Malicious code
- Dependencies locked (`uv.lock`); `pip-audit --strict` fails CI on known vulnerabilities; bandit in CI; no third-party scripts or CDNs (CSP `script-src 'self'`). **Met.**

### V11 Business logic
- 11.1.1–11.1.3 Sequential steps enforced: the election state machine is enforced by the application and again by database triggers (`tests/test_elections.py`); one person one vote under 1,000 parallel duplicates (`test_1000_parallel_duplicate_submissions_produce_exactly_one_ballot`); closing-time race (`test_submissions_racing_the_closing_time`). **Met.**
- 11.1.4 Anti-automation: login is throttled, and ballot submissions are limited per member per minute without ever giving a false answer about acceptance (`test_ballot_submissions_are_rate_limited_per_member`, `test_a_throttled_retry_is_never_told_a_false_not_recorded`). **Met (F-1 fixed).**

### V12 Files and resources
- 12.1 Size limits; 12.2 type checked from content; 12.3 random storage names outside any served directory; 12.5 downloads as attachments with a sandboxing CSP (`tests/test_nominations.py`). **Met.**
- 12.4.2 Malware scanning: **Gap (F-4)**.

### V13 API and web services
- No public API. All state-changing requests are POST with CSRF protection; the one JSON-producing view (result document) is read-only and permission-checked. **N/A / Met.**

### V14 Configuration
- 14.1 Build: locked dependencies; container runs as a non-root user (`Dockerfile`). **Met.**
- 14.2 Dependencies: see V10. **Met.**
- 14.4 HTTP security headers: strict CSP without `unsafe-inline`, `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy`, CORP and COOP (`test_security_headers`). **Met.**
- 14.5 Request validation: allowed hosts from the environment; Host header never used to build links (`SITE_URL`). **Met.**
- Host and container hardening, secrets store: **Deployment**.
