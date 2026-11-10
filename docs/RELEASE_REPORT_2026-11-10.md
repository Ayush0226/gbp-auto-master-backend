# GBP Auto Master: live repair and verification report

Updated 10 November 2026. This report supersedes the live-status statements in the 8 November diagnostic and 9 November roadmap. It distinguishes deployed repairs from checks that still need an external account or a designated test business. No claim of issue-free operation is made.

## What changed for users

1. The website and backend now run the credit-free application. Usage balances, top-ups, debits and credit-based feature gates have been removed from active website/API/MCP behavior. Historical financial rows remain archived; they do not control usage.
2. Razorpay checkout is temporarily disabled. Checkout scripts and the runtime dependency were removed. Old payment initiation/verification endpoints return an unavailable response.
3. The launch code **ATYAUNSUHJ** grants an authenticated user the yearly plan with lifetime access to currently supported features, no expiry and no renewal payment. Redemption is transactional and repeat-safe. It removes commercial location/keyword limits for that entitlement. It does not override Google ownership, publishing rules, operational safety limits or external-provider quotas. Existing users were not automatically upgraded or downgraded.
4. The screenshot's narrow token-history screen has been replaced by a responsive Plans & access page. Free, loading, failure and lifetime displays were checked at widths down to 320 pixels.
5. Image Studio is built into campaign creation: describe an image, generate a preview, select it, choose business locations and schedule. A normal photo upload remains available. Cloudflare's shared allowance is the chosen provider; actual generation remains pending its account configuration.

## Existing user access was preserved

Only the **GBP AI** Supabase project and the GBP backend in **Ayush's Render workspace** were changed. The Instagram database and unrelated services were left alone.

Before/after checks match for Auth user IDs and application-role metadata, location ownership, Google account ownership, and all pre-existing database policies. Existing subscription statuses and plan types were preserved. No customer's Google consent was revoked. Three existing Google grants were validated against their immutable Google identities, encrypted in a backend-only table and removed from editable Auth metadata. All three still refresh and read their Google accounts successfully.

The application-data backup contains 139 public-table rows and three Auth records, encrypted outside both repositories. Encryption/decryption was verified. This is an application snapshot, not a rehearsed full database disaster restore.

## Cron recovery and current schedules

The publishing and review jobs had been blocked since 4 October. Their original lock records were copied into run history before the verified stale locks were released. No campaign or review delivery was in flight. The old calendar post, “The Summer demo”, was moved to **needs_review** after a Google read check found no matching delivery in the inspected results. Its content and original due time were retained. It was not published or rescheduled.

Controlled authenticated runs of publishing, review processing and cleanup all returned HTTP 200. They published zero posts/replies and deleted zero images. Automatic Supabase dispatches subsequently returned HTTP 200 for publishing, review processing and monitoring. Cron SQL success and the actual HTTP worker response are recorded separately.

| Work | Active cadence | What it does |
| --- | --- | --- |
| Scheduled publishing | Every 5 minutes | Processes due approved content; holds content over an hour late for review instead of dumping an old backlog publicly. |
| Review processing and polling fallback | Every 10 minutes, offset by 2 minutes UTC | Processes queued replies and checks rule-managed locations when webhook events are missing. Existing enabled/approval rules remain authoritative. |
| Media cleanup | 02:00 IST daily | Considers old legacy published media after 30 days; skips registered assets and pending references. |
| Operational health check | Every 10 minutes, offset by 4 minutes UTC | Reports failed/stale jobs and held deliveries; unhealthy checks return HTTP 503. |
| Result collection | Every minute | Copies transient HTTP outcomes into durable history and removes expired operational history. |

Supabase Cron and pg_net call Render with a secret read from Vault. The secret is not embedded in schedule text. These jobs use the existing database; no paid Render cron service or worker was purchased. Each app job has a five-minute fenced lease and a 30-second heartbeat. A crashed worker can lose its lease while delivery-level guards remain held, preventing blind public retries.

Schedules run in UTC; the table above explains the user-facing time. Publishing can begin on the next five-minute tick plus processing/provider delay. It is not a guarantee of publication at an exact second. Current failure visibility is the admin Operations view and recorded dispatch results; no external email/phone alert destination is configured.

## Status against every original diagnostic finding

“Deployed” means matching application/schema changes are live. It does not imply that a public Google write was tested against a customer business.

| Finding | Status and resulting behavior |
| --- | --- |
| P01: stuck publishing cron | Reconciled and recovered; controlled and scheduled runs return 200. |
| P02: stuck review cron | Reconciled and recovered; controlled and scheduled runs return 200. |
| P03: overdue post | Safely held as needs_review; owner must decide its future content/time. |
| P04: misleading health | Dependency-aware readiness and separate job freshness checks deployed. Render's existing service still needs its platform health-check path set to /api/ready. The repository configuration contains that setting. |
| P05: unused Google connection table | Three validated encrypted grants and three connected summaries now exist. No usable grant remains in account metadata. |
| P06: notification delivery uncertain | Google notification settings read successfully; two identities have review topics and one has no locations/topic. Actual Pub/Sub subscription delivery/IAM remains unverified. Polling fallback is deployed and scheduled. Empty event tables alone do not prove delivery failure. |
| S01: permanent locks | Fenced leases, heartbeat, expiry and run history deployed. Recovery tests preserve uncertain delivery guards. |
| S02: completion timestamps | Successful and failed lease completion updates timestamps. Verified on live runs. |
| S03: accepted post retried after DB failure | Acceptance and subsequent persistence errors are separated; ambiguous delivery remains held. Fault tests pass. |
| S04: cancellation race | Parent cancellation and child claims serialize on the same campaign row. Lost/cancelled claims do not call Google. |
| S05: uncertain-delivery repair workflow | Read-only Operations view and reconciliation script exist. A complete operator repair UI is still future work; held jobs require evidence-based individual investigation. No held public delivery currently exists. |
| S06: daily reply limit race | Atomic publication-day enforcement in the business timezone is deployed. |
| S07: pause behavior | Workers recheck current rule state. Disabled rules stop pending automatic publication; switching to approval returns automatic queued replies to approval. |
| S08: webhook-only rules | Bounded polling fallback shares the authoritative event/rule pipeline and is active in review cron. |
| S09: shared-account ownership overwritten | Atomic ownership-conflict rejection deployed. Existing account/location owners were retained. Full multi-user membership support remains a separate feature. |
| S10: editable refresh grants | Three existing grants moved to encrypted backend storage after identity validation; refresh/read checks pass for all three. |
| S11: prepaid duration replaced | Settlement logic preserves remaining access; live checkout is paused. Actual payment capture is deliberately untested. |
| S12: inconsistent plan limits | Website/account/location/MCP reads use effective entitlements. |
| S13: inconsistent expiry | Read-time entitlement normalization is deployed; lifetime expiry is null. Stored existing statuses were preserved during migration. |
| S14: single-page reviews | Website pagination and bounded multi-page batch traversal are deployed; repeated cursors stop safely. |
| S15: analytics nesting | Nested Google metric series are normalized before totals; fixture checks cover real response structure. |
| S16: silent HTTP-200 failures | Inspected integration failure paths use error statuses and visible UI errors. Upstream transport failures return temporary service errors rather than forcing logout. |
| S17: repeated refresh/silent sync | Bounded Google refresh, single in-flight refresh and session-refresh cooldown deployed. Failed location refresh does not recurse indefinitely. |
| S18: unguarded provider work | Shared account-level rate/concurrency guards exist independently of paid usage credits. |
| S19: immediate post retry duplicates | Stable user-scoped IDs, request fingerprints and stored successful-result replay deployed. |
| S20: rank request collisions/replay | User-scoped IDs and shared website/MCP report-result replay deployed; replay issues a fresh download link. |
| S21: arbitrary timezones | IANA timezone validation deployed. |
| S22: trusting media metadata | Ownership/path checks, actual byte decoding, dimensions/size checks and JPEG normalization deployed; owned durable assets enter scheduling. |
| S23: inconsistent video support | Unsupported video publication is consistently disabled across inspected paths. |
| S24: incomplete infrastructure config | Runtime secrets/origins/URLs inspected; scheduler is now explicitly configured in Supabase. Render health-path activation remains outstanding. |
| S25: payment webhook reconciliation | Deferred while payments are disabled. Must be implemented before checkout returns. No stuck live billing order was found. |
| S26: UI-local backend secrets | Unnecessary backend secrets removed from the UI local environment; encrypted original retained outside Git. |
| S27: large public 3D bundle | Still approximately 887 kB minified in a lazy-loaded chunk. Slow-device performance work remains. |
| S28: lint warnings | Build/type checks pass; lint has no errors but still reports React hook/state/purity and unused-catch warnings. They are not all resolved. |
| S29: stale installed MCP skill | Updated version 0.4.0 source package is committed with no credit instructions. The local Personal marketplace source was backed up and updated; the standard installer confirms 0.4.0 installed and enabled. Restart Codex/start a fresh chat to load it. Other clients need their own update. |
| S30: incomplete handwritten tool list | Full 20-tool contract generated from runtime schemas and committed. |

## Additional issues found during release

- Old SDK/HTTP connection reuse caused dashboard requests to fail with RemoteProtocolError in Render logs. The Supabase upgrade uses bounded HTTP/1 connections without idle keep-alive reuse; automatic PostgREST write retries are disabled to avoid repeating side effects.
- Supabase admin user-list responses omit identities. The Google migration now fetches each full Auth record before checking immutable identity ownership. All three grants migrated successfully without reconnection.
- Sign-up and the public brochure still advertised paid actions/old renewal prices. Their copy now matches launch-code access and paused checkout. Brochure header and grid minimum widths also caused mobile overflow; layouts were corrected and checked at 320, 390, 768 and 1440 pixels.
- An old static panorama poster shadowed the /brochure application route in production. It was preserved as brochure-panorama.html; /brochure now serves the app, and the old brochure.html URL redirects to it.

## Dependency and database security results

The seven affected backend package families were updated, runtime dependencies constrained, Python 3.11.18 selected for Render, and Supabase upgraded to 2.34.1. The fresh local scan of 91 installed packages found no matching OSV advisories. The UI audit found zero known vulnerabilities. These are advisory checks, not proof that no security defect exists.

Supabase health advisors report no health findings. Missing foreign-key indexes were added, including five on the new release tables; the current missing-FK advisory count is zero. No existing policy expression was edited to achieve this.

Remaining database notices:

- Twelve backend-only tables have RLS with no client policy. This is intentional denial, not a request to open them. Direct checks confirm anonymous/signed-in clients cannot select protected credentials or access codes, and signed-in clients cannot dispatch cron. The backend runtime cannot execute the retired debit RPC. [Supabase explanation](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy).
- An existing rls_auto_enable event-trigger function has broad execution grants and is flagged for anonymous/authenticated access. Its event-trigger return type prevents normal direct invocation; this is still worth hardening in a reviewed permissions change. Existing grants were retained under the user's access-preservation requirement. [Anonymous function advisory](https://supabase.com/docs/guides/database/database-linter?lint=0028_anon_security_definer_function_executable).
- Leaked-password protection remains disabled in the existing Auth configuration. Enabling it would change password acceptance rules, so it was not silently changed. [Supabase password protection](https://supabase.com/docs/guides/auth/password-security#password-strength-and-leaked-password-protection).
- Forty-one per-row Auth-policy evaluation notices and twenty-six overlapping-policy notices remain. They concern existing policies; optimization must preserve and independently test access semantics. Thirty-seven unused-index notices include newly created indexes and are not, by themselves, grounds to delete them. [Policy evaluation advisory](https://supabase.com/docs/guides/database/database-linter?lint=0003_auth_rls_initplan), [overlapping policy advisory](https://supabase.com/docs/guides/database/database-linter?lint=0006_multiple_permissive_policies).
- The database still runs its existing PostgreSQL 17.6 patch version. A managed database patch/upgrade and a full restore rehearsal remain maintenance tasks, rather than unannounced production restarts.

## Cloudflare images: selected setup and practical limits

The chosen practical free option is **FLUX.2 Klein 4B on Cloudflare Workers AI**. Cloudflare currently includes 10,000 neurons daily. At the documented 26.05 neurons per output 512-pixel tile, a text-only 1024-pixel square costs approximately 104.2 neurons: roughly 95 such images/day if no other model uses the same allowance. This is an estimate, shared by the whole Cloudflare account, not 95 images per GBP user. Free-plan requests stop when the allowance is exhausted; a paid plan can incur charges. [Current pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/), [model documentation](https://developers.cloudflare.com/workers-ai/models/flux-2-klein-4b/).

To activate the implemented integration:

1. Open Cloudflare Workers AI and choose **Use REST API**.
2. Create the prefilled Workers AI API token and copy the Account ID. A custom token needs Workers AI Read and Edit for the intended account.
3. In the existing GBP backend's Render Environment settings, save CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN. Keep the secret out of chat, source files and browser settings.
4. Save and deploy; then verify the image-provider readiness and generate one preview before relying on it for campaigns.
5. Users open the content calendar, create a campaign, describe a photo, generate a preview, select it, pick locations/time and schedule. Users need no separate Cloudflare login with this shared setup.

These account values are not currently connected, so no real Cloudflare generation has been claimed or tested. [Official account/token setup](https://developers.cloudflare.com/workers-ai/get-started/rest-api/).

An optional Pollinations account-connect integration is also implemented using OAuth with PKCE and server-stored credentials; it stays unavailable until an app key/redirect is registered. It spends the user's approved provider allowance/budget, has expiring grants and is not an unlimited anonymous free service. The user selected Cloudflare instead. [Provider account-connection specification](https://github.com/pollinations/pollinations/blob/main/BRING_YOUR_OWN_POLLEN.md).

## Checks completed and remaining gates

- 93 backend tests pass, including public-delivery ambiguity, cancellation, encryption, identity mismatch, media validation and bounded pagination checks.
- 103 PostgreSQL/PGlite assertions pass, including lifetime redemption, account isolation, concurrency claims, expired leases and recovery. These simulate database behavior; no production process was intentionally killed.
- UI build/type checks pass. Responsive browser checks cover Plans & access at six widths and the image-preview-to-schedule journey on desktop/mobile with provider fixtures. They do not establish real provider-account consent or real public Google delivery.
- Live backend health/readiness, website delivery, protected MCP challenge, OAuth discovery, Google refresh/accounts reads, database REST access, Groq authentication and SerpApi account reads succeed. No paid rank scan was consumed; the observed provider balance is 250 searches.
- End-to-end MCP consent, expiry/refresh and revocation still need a designated test login/client. Existing customer grants have not been revoked as a test.
- Local GBP Master plugin 0.4.0 is installed and enabled through the supported installer. Restart the app/start a fresh chat before checking its new instructions and consent flow. [Official plugin update guidance](https://developers.openai.com/plugins/build/plugins).
- Genuine Pub/Sub push delivery/IAM requires the relevant Google Cloud subscription/log access. Connected Render/Supabase administration does not confer Google Cloud administration.
- A complete real public-write journey and crash/restart rehearsal need an isolated test business/environment. The user has been asked to identify a test profile; no customer business was chosen automatically.
- Actual image generation awaits the selected Cloudflare account setup.
- An external alert destination and Render's platform readiness path still need configuration. Current monitoring records errors and exposes unhealthy status; it does not send phone/email messages.

## Further cron improvements available

For stronger production reliability, move the free Render web service to approved always-on compute, add a dedicated worker and durable queue/dead-letter handling, monitor provider quotas and latency, add an operator reconciliation screen, rehearse process termination with test content, and configure an external missed-job alert destination. Costs and a concrete infrastructure plan should be reviewed before provisioning paid resources.

Render documents idle shutdown, monthly free-instance hours and suspension limits on free services, and advises against using them for production applications. The current five-minute scheduler improves processing cadence but does not remove the platform's free-tier constraints. [Render free-service limits](https://render.com/docs/free).
