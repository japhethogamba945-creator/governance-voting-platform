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
