# Phase 7 verification report

**Build:** branch `phase-7-verification`, on top of `main` with increments 6.1–6.8.
**Scope:** BLUEPRINT §9, Phase 7: the 17 acceptance tests, concurrency stress and fault injection, counting against an independent reference, restore from backup, the leak canary, an OWASP ASVS Level 2 checklist, and the election lifecycle on mobile browsers.
**Data:** fictional organisations, members and candidates only.

> This report records what the automated tests and the development team's own checks show. It is **not** an independent security assessment or certification. The Phase 8 gate requires an independent penetration test and cryptography review, the manual tests run on a staging deployment, and testing on real mobile devices.

## Result

- **Automated suite:** 340 tests pass against a real PostgreSQL 16 with the production least-privilege roles and row-level security. ruff, the migrations check, bandit and `pip-audit --strict` are clean.
- **Mobile lifecycle (acceptance test 17):** the complete lifecycle passes in Chromium emulating a Pixel 7 (412×839) and in WebKit 26.0 emulating an iPhone 14 (390×664), both in CI (job `e2e`, 11 s and 18 s); Chromium also locally.
- **Acceptance tests:** 1–16 have automated evidence. 17 has automated evidence in emulation; real-device testing is still to be done.
- **Findings:** five, none affecting ballot secrecy or one-person-one-vote: F-1 to F-5 in the [ASVS self-assessment](asvs-l2.md#findings-to-resolve-before-the-pilot). They must be resolved or accepted before the pilot.

## Acceptance tests

| # | Requirement | Evidence | Result |
|---|---|---|---|
| 1 | Two organisations cannot access each other's data | Every organisation URL is discovered automatically and checked for cross-tenant and object-level access with identical 404s (`test_tenant_isolation_http.py`, 116 cases); database level (`test_tenant_isolation_db.py`); RLS forced on every tenant table (`test_rls_catalogue.py`); privileges equal the manifest (`test_grants.py`) | Pass |
| 2 | Eligible members can access their assigned elections | Dashboard and ballot for electorate members (`test_voting.py::test_ui_vote_review_cast_receipt`); mobile lifecycle step 5 | Pass |
| 3 | Ineligible members cannot vote through direct requests or manipulated interfaces | `test_members_outside_the_electorate_cannot_vote`, `test_ui_ineligible_member_is_refused`, `test_ineligible_people_and_duplicates_are_refused` | Pass |
| 4 | Concurrent duplicate submissions produce at most one accepted ballot | **1,000** submissions for one voter, 50 connections at a time: exactly 1 ballot (`test_1000_parallel_duplicate_submissions_produce_exactly_one_ballot`, 9.5 s); 200 on 20 connections (`test_voting.py`) | Pass |
| 5 | Submissions outside the voting window are rejected | `test_votes_outside_the_voting_window_are_refused`; **closing race**: 40 voters submitting across the closing time while the scheduler closes; every accepted ballot is in the sealed box, the rest are refused, ballots equal consumed entitlements (`test_submissions_racing_the_closing_time`, run 5× without failure) | Pass |
| 6 | A member cannot exceed the selection limit | `test_invalid_selections_are_rejected_and_nothing_is_recorded`; re-validated at count (`test_count_matches_a_reference_tally_and_flags_invalid_ballots`) | Pass |
| 7 | A receipt confirms acceptance without exposing choices | `test_ui_vote_review_cast_receipt`, `test_browser_made_receipt_commitment_is_used`; browser-only receipt check (mobile lifecycle step 9) | Pass |
| 8 | Administrative views and ordinary audit records do not disclose choices | `test_admin_and_audit_views_disclose_no_choices`, `test_no_choice_leaks_into_logs_audit_email_or_sessions`, `test_marked_register_shows_who_voted_but_never_how`, `test_counting_never_records_how_anyone_voted` | Pass |
| 9 | The final tally matches an independently calculated result | Seat allocation vs an independent definition on 3,000 random elections (`test_allocation_matches_an_independent_reference_on_random_elections`); 200-ballot count vs a reference tally (`test_count_matches_a_reference_tally_and_flags_invalid_ballots`) | Pass |
| 10 | Partial ballots and permitted abstentions are counted correctly | The reference count includes blank and partial ballots; `test_partial_and_blank_ballot_rules` | Pass |
| 11 | Ties are flagged and handled by the approved procedure | `test_tie_for_the_last_seat_must_be_resolved_before_certification`, `test_database_checks_tie_resolutions_independently` | Pass |
| 12 | Closed elections cannot be modified without authorised intervention | `test_configuration_locked_after_submission_and_closed_is_final`, `test_history_cannot_be_written_or_altered_by_the_app`, ballot box immutable even for the owner (`test_web_role_cannot_read_or_write_the_ballot_box`, `test_ballot_box_cannot_be_truncated`), tally and certification guards | Pass |
| 13 | Recounts preserve the original tally and document the new outcome | `test_recount_keeps_earlier_counts_and_needs_a_reason`, `test_dispute_recount_supersedes_the_published_result` | Pass |
| 14 | Certified results cannot be silently altered | `test_tallies_and_certifications_are_immutable` (even the superuser is stopped by triggers); audit chain tamper detection, including a rewritten chain (`test_audit_chain.py`); document fingerprint recomputed by "Election checks" | Pass |
| 15 | An interrupted request cannot produce a duplicate ballot or a false success | **Connection killed before COMMIT** (`pg_terminate_backend`): nothing recorded, retry accepted once (`test_connection_killed_before_commit_records_nothing_and_a_retry_succeeds`); **response lost after COMMIT**: 50 retries all answered "accepted", one ballot (`test_response_lost_after_commit_retry_is_answered_truthfully`); application-level failure before commit (`test_voting.py`) | Pass |
| 16 | Backup restoration and essential security controls are tested | **Restore drill** with `pg_dump`/`pg_restore` and `post_restore_check`, including a recount on the restored copy and detection of damage (`test_restore_drill.py`, [procedure](../operations/backup-and-restore.md)); security controls: the suites above and the [ASVS self-assessment](asvs-l2.md) | Pass (automated); drill on real infrastructure pending |
| 17 | The complete lifecycle works on common mobile browsers | `e2e/run.sh`: 10 steps through the real pages, six people on separate emulated phones (below) | Pass in Chromium and WebKit emulation; real devices pending |

## Mobile lifecycle (acceptance test 17)

`e2e/lifecycle.mjs` drives the real pages with Playwright, one browser context ("phone") per person:

1. The administrator creates the election, saves rules and opens nominations.
2. Two members nominate themselves.
3. The Returning Officer moves to review, approves both nominations, submits for approval, and schedules with the key ceremony (two other keyholders).
4. Time is simulated (`e2e/clock.py`) and `manage.py run_scheduler` opens voting.
5. Three members vote. Each gets a receipt code made in the browser, the receipt page shows no choice, and a second visit says "already accepted".
6. The scheduler closes voting and seals the ballot box.
7. The Returning Officer starts the count; the administrator and the Auditor each provide their share from their own phone.
8. The Returning Officer certifies; the administrator publishes.
9. A member sees the result and finds her receipt in the browser-only check.
10. The Auditor verifies the audit chain and sees "All checks passed".

Each phone signs in with a password and an authenticator code, and privileged steps go through step-up when asked. The script also asserts that no page scrolls sideways at phone width, and that there are no browser errors and no failed requests (only the browser's own favicon request is ignored).

| Browser | Device emulation | Where | Result |
|---|---|---|---|
| Chromium 141 | Pixel 7, 412×839 | local and CI | Pass (15 s locally, 11 s in CI) |
| WebKit 26.0 | iPhone 14, 390×664 | CI (`e2e` job) | Pass (18 s) |

Emulation is not the real thing: real devices, low-end hardware, Samsung Internet and poor networks remain manual test 12.

## Restore drill (acceptance test 16)

Output of `post_restore_check` on the restored copy:

```
OK   migrations applied (0 pending)
OK   privileges match the manifest (0 missing, 0 unexpected)
OK   row-level security forced on every tenant table
OK   no application role is superuser or bypasses RLS
OK   stress-…: audit chain (27 events, 1 anchors, 0 not yet chained)
OK   stress-…: election 'Board 2026' fingerprints
All post-restore checks passed.
```

The recount on the restored copy reproduced the original totals and ballot-box digest. After the drill damaged one audit event, the check failed as intended.

## Not covered here (Phase 8)

- An independent penetration test and cryptography review.
- The manual security tests on a staging deployment ([list](manual-security-tests.md)).
- Real-device testing: Chrome on a low-end Android phone, Safari on iOS, Samsung Internet.
- The restore drill on the real backup infrastructure, synchronous replication, and a load test at twice the expected peak (05 §9).
- Findings F-1 to F-5, and the open **[CONFIRM against pilot governing documents]** and **[LEGAL-REVIEW]** items in `docs/design/`.
