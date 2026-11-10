# Deploy the removal of usage credits

> Historical cutover instructions. The matching backend, website and migrations are now deployed. Follow [the 10 November release report](RELEASE_REPORT_2026-11-10.md) for current status. Do not re-enable payment checkout or rerun the original identity/role migration as part of follow-up work. The live Render platform health path still needs activation; a value in render.yaml alone does not apply it to an unmanaged service.

> Updated 9 November: payment checkout is now paused and lifetime access codes replace plan checkout. Apply migration 005 after 004 and follow `LIFETIME_ACCESS_2026-11-09.md` for the current rollout. Payment-checkout steps below describe the previous cutover plan.

Prepared 8 November 2026. These changes are local and have not been deployed.

## Result

The website, REST API and MCP no longer sell top-ups, grant welcome credits, expose a credit balance tool, check balances or charge for replies, rank reports and scheduled content. Prepaid subscription prices and plan features remain. Google OAuth, Supabase sessions, MCP OAuth and signed PDF links remain authenticated.

Historical balances, ledgers and payments remain stored for accounting and support. They do not control active features. Removing financial history is a separate data-retention decision.

## Coordinated rollout

1. Take a database backup. Pause external scheduler invocations and drain backend requests during the cutover. The old backend must not run against the new database functions.
2. In Supabase apply `migrations/004_retire_usage_credits.sql` after migrations 001–003. It creates `operation_runs`, carries forward completed and uncertain delivery guards, replaces scheduling and subscription settlement, disables legacy credit RPCs and removes monthly grants. It does not delete old financial records.
3. Deploy the matching backend and UI from these working copies together. The backend requires migration 004 at startup. The Render health check now uses `/api/ready`; `/api/health` remains a lightweight process check.
4. Check Render environment values: Supabase URL/service role, Google client ID/secret, Groq key, Razorpay keys, CRON_SECRET, SERPAPI_KEY for rank scans, ALLOWED_ORIGINS, MCP_RESOURCE_URL and PUBLIC_API_URL. Pub/Sub additionally needs GOOGLE_PUBSUB_TOPIC, GOOGLE_PUBSUB_AUDIENCE and GOOGLE_PUBSUB_SERVICE_ACCOUNT. Never put service-role or Google secrets in VITE-prefixed settings.
5. Refresh the MCP client's tool schema and update the GBP Master plugin instructions. Remove the balance tool, credit prices, refunds, welcome grants and top-up instructions. The locally installed cached skill still describes the old credit model; redeploy its source package rather than editing its cache.
6. Reconcile the two old `job_runs` entries before resuming cron. Both publishing and replying were still marked running from 4 October. Stop/drain workers, verify Google state and the overdue 5 October post, then release only the verified stale job locks. Do not blindly run the overdue queue: that could publish old content.
7. Resume the verified scheduler and inspect job results and Google state. Confirm the scheduler's actual URL, frequency, timezone and bearer secret in its administration console; these are not fully represented by `render.yaml`.

## Verification after deployment

- `/api/ready` returns 200 when the required database schema is available and 503 when it is unavailable.
- Anonymous calls to private API routes, cron and MCP are rejected.
- A zero-balance account can draft, publish an approved reply, schedule content and generate a rank report. No new debit or grant appears in historical ledgers.
- Replaying a completed public reply is blocked. An uncertain Google delivery remains held for reconciliation.
- Top-up endpoints and `/api/tokens/*` are absent; MCP no longer advertises `get_credit_balance` or credit response fields.
- A subscription payment settles once and extends prepaid access without issuing usage credits. Existing pending historical top-up payments require support reconciliation; none were present in the scanned live `billing_orders` table.
- Test one approved review and one approved post against a designated test business, not a real customer's profile without approval.
- Confirm review webhook delivery, delayed jobs, storage policies, and the scheduler using authenticated administration logs.

## Rollback

Do not roll back only the application: migration 004 revokes the old debit RPCs and the old app will fail. Pause jobs and use the database backup plus matching previous backend/UI as one rollback unit. Review all post-cutover public deliveries first; restoring a database cannot undo content already accepted by Google.
