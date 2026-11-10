# Work remaining for a dependable release

> Historical plan. For deployed fixes, recovered cron schedules, preserved user access and the current remaining gates, read [the 10 November release report](RELEASE_REPORT_2026-11-10.md).

Based on the 8 November diagnostic, updated for the 9 November payment/lifetime changes. This is the remaining work plan; it does not claim that the live failures have been repaired. The earlier diagnostic's production observations are dated 8 November and must be rechecked during recovery.

## Already implemented locally

- Usage-credit retirement across the backend, website, MCP and database migration 004.
- Compatible UI dependency security updates; the last UI audit reported zero known vulnerabilities.
- Dependency-aware readiness and improved cron completion timestamps.
- Per-account rank request IDs.
- Temporary payment pause and lifetime launch-code entitlement in migration 005, including unlimited keywords and locations for that entitlement.
- Consistent effective subscription reads for the account profile, location profile, platform overview and MCP. This addresses stale plan limits and expired/lifetime display inconsistencies (S12/S13) in these paths.
- Removal of the two unused payment-dashboard implementations, the global payment script, and duplicate environment entries in render.yaml.
- Onboarding identity correction, discovered while adding launch redemption.

None of these local changes are live until migrations and matching application versions are deployed.

## Phase 1 — Recover production safely and deploy the matching versions

| Work | Report references | Acceptance gate |
| --- | --- | --- |
| Inspect Render releases/worker logs and the scheduler provider; establish actual cadence, URL, timezone and secret. | P01/P02, S24 | Every required job has a known schedule and authenticated success/failure history. |
| Reconcile the two job locks still running from 4 October; inspect actual Google deliveries before releasing them. Review the overdue 5 October post with its owner. | P01/P02/P03, S01 | No stale running lock; no unreviewed old post published during recovery. |
| Deploy migrations 004 and 005 plus the matching backend/UI, using a backup and a coordinated maintenance/drain window. | Credit retirement, launch access | Readiness passes, payment endpoints are paused, code redemption grants permanent account-wide access, and credits no longer control actions. |
| Verify health and add separate cron freshness/failure alerts. | P04, S02 | A database outage fails readiness; a missed/stuck job is visible without relying on `/api/health`. |

## Phase 2 — Make public publishing safe under failures and concurrency

| Work | Report references | Acceptance gate |
| --- | --- | --- |
| Replace permanent job locks with durable ownership/lease/heartbeat handling and safe crash recovery. Reclaiming a worker must not blindly retry uncertain public deliveries. | S01/S05 | A killed worker can recover without duplicate posts or permanently blocking all later runs. |
| Distinguish Google rejection, unknown delivery and accepted delivery before updating other records. An audit/database failure after acceptance must never make a campaign eligible for blind replay. | S03/S05 | Fault-injection tests cover timeout and database failure immediately before/after Google acceptance. |
| Serialize campaign cancellation with child delivery claims, and recheck publication eligibility at the delivery boundary. | S04 | A cancelled campaign cannot start another public delivery; an already accepted delivery is recorded accurately. |
| Enforce daily automatic reply limits atomically in the chosen business timezone; define pause behavior for already queued replies. | S06/S07 | Concurrent review events cannot exceed the configured rule, and pause behaves as the UI describes. |
| Add stable user-scoped request IDs and stored-result replay for immediate posts and rank reports. | S19/S20 | Retrying the same request neither republishes content nor repeats an already completed provider scan. |
| Provide a controlled operator reconciliation view for uncertain or failed jobs. | S05 | Operators can inspect evidence and recover a specific job without blanket resets. |

## Phase 3 — Secure connections and make data accurate

| Work | Report references | Acceptance gate |
| --- | --- | --- |
| Move Google refresh grants from editable user metadata into protected server storage and complete google_connections as the connection authority. Remove the unnecessary backend secrets from the UI project's local environment. | P05, S10/S26 | Browser/account responses never expose refresh grants; reconnect, refresh and revoke work against the protected store. |
| Model shared Google account membership, or reject cross-user account ownership conflicts atomically before upserting summary rows. | S09 | Connecting a shared account cannot move another user's account summary or access. |
| Inspect Pub/Sub topic/subscription/IAM, push audience and service-account identity. Add a polling fallback for rule-managed locations. | P06, S08 | A genuine test review reaches the inbound event and reply queue; disconnecting webhook delivery cannot silently stop all review processing. |
| Add website review pagination and a bounded backlog traversal for batch replies. | S14 | Older unanswered reviews remain discoverable even with more than one Google results page. |
| Normalize the analytics response nesting and use real API fixtures. | S15 | Nonzero Google metrics appear correctly, with no invented zero totals. |
| Use consistent HTTP errors, visible UI failures and bounded Google refresh retries. | S16/S17 | Expired authorization produces a reconnect path, upstream errors do not show success, and retry loops terminate. |
| Validate timezone identifiers and media ownership/content/HTTPS durability. Apply one supported video policy to all publishing paths. | S21/S22/S23 | Invalid inputs are rejected before jobs enter the queue; unsupported formats cannot be scheduled through a legacy path. |
| Add account-level rate and concurrency controls independent of commercial limits. | S18 | Lifetime access has no billing quota, while runaway parallel requests and abusive loops cannot exhaust the service. |

## Phase 4 — Security maintenance, MCP verification and release gates

| Work | Report references | Acceptance gate |
| --- | --- | --- |
| Update and retest the seven backend packages identified by OSV; distinguish runtime packages from pip/setuptools. Lock transitive dependencies and confirm Render's actual installed versions. | Backend dependency findings | Fresh environment passes dependency checks, advisory review and all tests. |
| Refresh the GBP Master plugin instructions and generate the full MCP tool contract from runtime schemas. Test OAuth consent, expiry, refresh and revocation across supported clients. | S29/S30 | No stale credit instructions; the website and MCP show the same effective lifetime access and ownership. |
| Resolve remaining lint warnings, check responsive layout/accessibility and reduce the heavy public 3D bundle if measurements show a problem. | S27/S28 | Type/build/lint gates meet the agreed standard; mobile journeys and slow connections are usable. |
| Verify database RLS/grants, backup restore, secret handling, provider quotas and service restart behavior in staging/management logs. | Diagnostic verification gaps | Cross-account negative tests and recovery rehearsal pass. |
| Run a complete staging journey: new user → code → Google connection → locations → reply draft/approval/publication → scheduled campaign → rank PDF → MCP → disconnect/reconnect. Use a designated test business. | End-to-end release gate | The complete journey works, and injected failures are visible and recoverable. |

Payment webhook reconciliation (S25) is deferred while checkout is paused. Before payments return, it becomes a release requirement: captured payments must reconcile even when the browser callback is lost, and lifetime grants must never be downgraded by a paid order.

The release target is verified behavior, safe recovery and actionable monitoring. A passing build alone cannot establish issue-free production operation. Render/Supabase administration access and Google Pub/Sub delivery logs are still needed for the live recovery and verification steps.
