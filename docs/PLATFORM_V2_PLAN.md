# GBP Auto Master platform plan

GBP Auto Master is an account-first platform. A signed-in user owns one subscription and one credit wallet, can connect one or more Google identities, and can manage any number of Google Business Profile accounts and locations exposed by those connections.

## Ownership model

```text
user
  subscription + credit wallet
  google connections
    GBP accounts
      locations
        reviews, posts, media, analytics and automation overrides
```

Every operation derives the user from the authenticated Supabase session. Client-supplied user IDs, Google tokens and Google refresh tokens are never accepted by MCP tools. Location-scoped writes verify ownership on the server.

## Delivery phases

### Phase 1: account and workflow foundation

- Move plan entitlement to `account_subscriptions` while retaining location subscription fields during migration.
- Record connected Google identities and the GBP accounts found through each connection.
- Add account preferences, including timezone and default location.
- Add review automation rules with account defaults and location overrides.
- Add content campaigns and one delivery record per target location.
- Add a media asset registry, webhook-event deduplication and an audit trail.
- Expose authenticated account overview, calendar and automation endpoints.

### Phase 2: content studio

- Draft, preview, schedule, reschedule, duplicate and cancel campaigns.
- Support `STANDARD`, `EVENT` and `OFFER` Google post types.
- Validate call-to-action URLs, event windows, offer fields and location timezones.
- Add direct photo upload and processing before Google delivery.
- Publish due campaign deliveries through an idempotent worker with bounded retries.

### Phase 3: review automation

- Configure Google Pub/Sub for review notifications.
- Deduplicate notifications and retrieve the authoritative Google review.
- Resolve the location rule over the account default.
- Draft with AI, apply safety checks and either queue approval or publish.
- Enforce daily limits, delays, rating ranges and blocked terms.
- Retain a complete audit record and charge only successful writes.

### Phase 4: media and performance

- Manage profile, cover, logo and categorized location photos.
- Feature-gate video until each Google endpoint is verified in production.
- Add daily performance metrics, search terms and location comparisons.
- Add campaign and review-response reporting.

### Phase 5: production hardening and public release

- Add notification delivery, operational dashboards, retry controls and dead-letter handling.
- Run multi-account and multi-location integration tests.
- Complete privacy, retention, deletion and Google API policy reviews.
- Version and submit the public plugin.

## Stable workflow rules

1. Read operations are free.
2. Drafting is free.
3. A write displays its targets, content, schedule, timezone and total credit charge before confirmation.
4. One campaign creates one independently tracked delivery per location.
5. Credits are reserved once with an idempotency key and finalized only after the documented action boundary.
6. Ambiguous Google delivery is never retried automatically until the remote state is inspected.
7. Cancellation does not refund the original scheduling charge.
8. Background jobs do not depend on an open browser or AI conversation.
9. Existing v1 routes remain available until the website and MCP clients have migrated.

## Deployment sequence

1. Apply `migrations/003_platform_foundation.sql` in Supabase.
2. Deploy the backend and verify `/api/health` plus the platform overview endpoint.
3. Deploy the website and exercise single- and multi-location accounts.
4. Refresh the MCP server metadata and test in a new Codex task.
5. Enable new background jobs only after their environment configuration is present.

