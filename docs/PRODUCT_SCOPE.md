# GBP Auto Master product scope

GBP Auto Master uses one account, subscription and credit wallet to manage every Google Business Profile account and location the user connects. The website and MCP plugin call the same authenticated service layer, so an action has the same validation, cost and result in either interface.

## End-to-end flow

1. The user signs in to GBP Auto Master and receives the one-time 200-credit account bonus.
2. Google OAuth stores a server-side connection and syncs the Google Business Profile account and location hierarchy.
3. The user selects an account, one or more locations, or an account-wide default.
4. A read or draft runs free. A public write shows its exact content, targets, time, timezone and total credit cost.
5. The server creates an idempotent job and reserves credits once.
6. A background worker publishes through the official Google API. A confirmed failure refunds a reservation; an uncertain network result waits for reconciliation.
7. The dashboard and MCP return the delivery result and retain an audit record.

## Modules and delivery order

| Module | Main capabilities | Current foundation | Next production work |
| --- | --- | --- | --- |
| Account | Shared wallet, plan, preferences, audit, many GBP accounts and locations | Schema and API implemented | Account switcher and deletion/export UI |
| Reviews | Read, draft, approve, schedule, publish, cancel, automation rules | Webhook queue, rules, daily limits and approval UI implemented | Notification delivery, retry/reconcile console |
| Content calendar | Draft and schedule STANDARD, EVENT and OFFER campaigns across locations | Campaign schema, charging, UI and publisher implemented | Reschedule, duplicate, recurring campaigns and previews |
| Media | Upload and register owned assets | Photo upload and asset registry implemented | Library UI, tags, reusable assets, Google photo categories and verified video publishing |
| Business information | Hours, special hours, phone, website, category, attributes and description | Account/location ownership model ready | Read/diff/approval tools and Google update adapters |
| Products and services | Catalog items, menus and service lists | Planned | Google API capability review, schema and editing flows |
| Questions and answers | Monitor and answer supported customer questions | Planned | Confirm current Google API availability before implementation |
| Performance | Views, calls, website clicks, directions and search terms | Existing analytics can be migrated | Daily snapshots, comparisons, exports and anomaly alerts |
| Reputation | Review trends, response time, sentiment and issue alerts | Review data available | Aggregations, alerts and reports |
| Operations | Webhooks, cron workers, dead letters, retries and health | Authenticated cron, dedupe and audit implemented | Reconciliation jobs, alerts and admin controls |

## Stable API sequence

### Review automation

`PUT /api/platform/automation-rules` configures the account default or a location override. A Google Pub/Sub event enters `POST /api/webhooks/google-reviews`, is deduplicated, fetches the authoritative review, applies the rule and creates a `review_reply_jobs` row. Draft and approval modes stop before publishing. Automatic mode schedules delivery after the configured delay. The review cron publishes due jobs and charges 2.5 credits only at the publishing boundary.

### Content campaign

`POST /api/platform/campaigns` creates a free draft and its location deliveries. `POST /api/platform/campaigns/{id}/schedule` validates a future zoned time and atomically charges 5 credits per location. The publishing cron claims due deliveries one at a time and records each Google result. `POST /api/platform/campaigns/{id}/cancel` stops unpublished deliveries without a refund.

### Media

The browser uploads an owned object under `{user_id}/...` and then calls `POST /api/platform/media` to register its type and public URL. A campaign references the asset by ID. Photo publishing is enabled. Video remains registered but cannot be attached to a local-post delivery until the exact Google video endpoint has passed production integration testing.

## MCP tools in version 1.1

The MCP exposes account and location discovery, shared credits, reviews and direct replies, automation-rule management, review-job approval, multi-location campaigns and the legacy single-location scheduled-post tools. Every tool derives ownership from OAuth. User IDs and Google tokens are never tool inputs.

## Deployment gates

1. Apply database migration `003_platform_foundation.sql`.
2. Deploy and health-check the backend.
3. Run one sandbox review notification and one single-location campaign.
4. Deploy the dashboard and test approval, cancellation and a two-location campaign.
5. Update and reinstall the local plugin, then test its authenticated MCP tool list in a fresh task.
6. Enable the production cron schedules.
7. Add remaining modules in the table above without changing the ownership, confirmation, charging or audit rules.
