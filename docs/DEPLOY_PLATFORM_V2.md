# Platform v2 deployment checklist

Deploy in this order. The backend intentionally refuses to start if migration 003 is missing.

## 1. Database

Open the Supabase SQL editor for the production project and run the complete contents of `migrations/003_platform_foundation.sql` once. Confirm these tables exist:

- `account_subscriptions`
- `gbp_accounts`
- `review_automation_rules`
- `review_reply_jobs`
- `media_assets`
- `content_campaigns`
- `campaign_locations`
- `inbound_events`
- `audit_events`

The migration is additive and keeps the v1 location subscription fields as compatibility mirrors.

## 2. Render settings

Set these variables on `gbp-auto-master-backend-us`:

```text
GOOGLE_PUBSUB_TOPIC=projects/<google-cloud-project>/topics/<topic-name>
GOOGLE_PUBSUB_AUDIENCE=https://gbp-auto-master-backend-us.onrender.com/api/webhooks/google-reviews
GOOGLE_PUBSUB_SERVICE_ACCOUNT=<push-auth-service-account-email>
```

Keep the existing `CRON_SECRET`, Google OAuth, Groq, Razorpay and Supabase values. The Pub/Sub push subscription must send an OIDC token whose audience and service-account email exactly match the two variables above.

Grant `Pub/Sub Publisher` on the topic to `mybusiness-api-pubsub@system.gserviceaccount.com`. Grant the push service account permission to mint the OIDC identity used by the subscription.

## 3. Backend

Merge the backend branch and wait for Render health checks. Verify:

```text
GET /api/health
GET /.well-known/oauth-protected-resource/mcp
```

With a signed-in browser session, verify:

```text
GET /api/platform/overview
GET /api/platform/automation-rules
GET /api/platform/campaigns
GET /api/platform/review-jobs
```

## 4. Background schedules

Invoke both routes with `Authorization: Bearer <CRON_SECRET>`:

```text
GET /api/cron/publish-scheduled
GET /api/cron/reply-reviews
```

Run them every minute if the hosting plan supports that interval; otherwise use the shortest supported interval. The database claim and per-delivery state prevent overlapping work.

## 5. Website

Merge and deploy the UI branch after the backend is healthy. Test:

1. Save an approval-mode review rule.
2. Confirm a notification creates one queue item and a replay creates no duplicate.
3. Approve the reply and confirm one 2.5-credit charge.
4. Upload one photo, create a two-location campaign and confirm the displayed 10-credit charge.
5. Schedule it, run the publishing cron, and inspect both location delivery results.
6. Cancel a separate scheduled draft and confirm no refund is shown.

## 6. MCP plugin

The local plugin source is version `0.2.0` with a cachebuster and is already reinstalled. After the backend deploy, open a fresh Codex task so it discovers MCP server version `1.1.0`. Confirm that `tools/list` includes account overview, automation rules, review jobs, media assets and content campaigns.

## Rollback

If a production check fails, roll Render and the website back to their previous commits. Do not remove migration 003 tables while investigating; v1 code ignores them, and keeping them preserves queued work and audit history.
