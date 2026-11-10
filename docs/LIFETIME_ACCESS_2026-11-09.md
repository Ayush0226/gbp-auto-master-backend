# Launch access and temporary payment pause

Prepared 9 November 2026. Implemented in the working copies; not deployed.

## User experience

Users sign in, open **Plans & access** (or the keyword step during onboarding), and enter **ATYAUNSUHJ**. A successful redemption gives that account the **yearly plan with lifetime access to all currently supported GBP Master features**. No payment, usage credits, renewal or expiry date is involved. Keyword and connected-location plan caps are removed for this entitlement. Google authorization, third-party availability, supported publishing formats, per-request validation and user-chosen automation rules still apply.

The code can be redeemed by multiple signed-in accounts. Each account redeems it once; repeat requests return the existing grant. Disabling the code stops new redemptions without expiring existing grants. No operator/admin permissions are granted.

## Implementation

- Migration 005 adds an explicit `is_lifetime` flag and `access_scope=current_features_v1`, with `expires_at=null`. This avoids pretending that a 12-month subscription or a far-future expiry is lifetime access.
- The code's SHA-256 hash is stored in a protected table. Browsers submit the entered code; they never receive a list of valid codes. The backend derives the account identity from the session.
- Redemption uses a transaction and an account lock, writes an audit record once, updates the account and legacy profile mirrors, and issues no usage credits.
- Website profiles, worker subscription reads, platform overview and MCP share effective entitlement behavior. An inactive/revoked account is not restored by retrying a past redemption.
- Keyword settings and onboarding recognize an unlimited entitlement. Onboarding now derives identity from the authenticated session; the previous screen omitted a required body user ID and could fail with HTTP 422.
- Razorpay imports, client creation, SDK dependency, required keys, public checkout script, checkout UI and old dashboard implementations are removed. Existing create/verify/key routes return HTTP 503 explaining that checkout is paused. Database settlement permission is revoked for the service role during the pause.
- Existing payments and historical billing records are retained. Lifetime code activations are not counted as paid monthly revenue in the administrator estimate.

## Deployment

1. Back up the database and drain application requests; pause external scheduled invocations during the cutover.
2. Apply migrations **004 then 005**, after migrations 001–003. Migration 004 removes usage credits; migration 005 adds lifetime redemption and pauses paid settlement.
3. Deploy this backend and UI together. Startup and `/api/ready` now require migration 005's entitlement columns. Razorpay keys are no longer required. Retained old secrets in an existing environment are unused; remove them from deployment settings when convenient.
4. Confirm a fresh account can sign in, redeem the exact code, see Yearly · Lifetime access, save more than 15 valid keywords and get the same entitlement through MCP. Test that retrying does not create a second grant or audit event.
5. Confirm another account cannot redeem on someone else's behalf or directly edit entitlement/code tables. Confirm create/verify/key endpoints cannot charge or settle a payment.
6. Complete the operational recovery and release gates in `RELIABILITY_ROADMAP_2026-11-09.md` before resuming publishing.

Applying only the database migration will break an older backend's payment behavior; deploy the matching versions as one cutover. Re-enabling payments later requires restoring and testing the gateway integration and database settlement permission together, with an explicit guard preserving lifetime grants. It is not a UI-only switch.

## Local verification

76 backend tests and 82 PostgreSQL/PGlite assertions pass. Checks cover invalid codes, normalized valid redemption, ownership, repeat redemption, code deactivation, preserved lifetime access, revoked-access handling, unlimited keywords, onboarding identity, MCP lifetime reads and disabled payment settlement. The production UI builds successfully and its package audit reports zero known vulnerabilities. Lint still reports warnings in the remaining screens; the roadmap tracks them.
