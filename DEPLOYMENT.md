# Deployment and verification

The changes in this checkout are **not deployed**. Apply the database migration and
the backend/frontend releases together. Do not publish only the frontend or only
the backend: the new API requires a Supabase session on every private request.

## Token model

- `user_profiles.tokens_balance` is the account's shared balance. All of its Google
  business profiles spend from that balance.
- Existing balances are retained. A new account gets 200 tokens once, transactionally.
  Linking or reconnecting another profile does not grant tokens.
- `account_token_ledger` contains account history, including copied legacy entries.
  Copying history does not replay credits.
- Plan entitlements remain per business profile. Plan purchases/monthly grants and
  top-ups credit the shared account. Top-ups and token promos require no selected profile.
- Review replies cost 2.5 tokens; competitor reports cost 10; scheduling a post costs
  5. Scheduling is charged when the row is created, not again when it is published.
- Plans are prepaid purchases. There is no Razorpay recurring subscription; cancellation
  therefore does not cancel a recurring charge. It leaves access until expiry.

## Rollout order

1. Export/back up `user_profiles`, `token_ledger`, `calendar_posts`, and existing
   Supabase auth metadata. Pause external cron jobs and briefly stop checkout/new
   writes during the cutover. The SQL migration is additive, but it deliberately
   revokes old browser write permissions.
2. Run **`migrations/001_secure_billing.sql`** in the Supabase SQL editor, once, as
   the project database administrator. The entire migration is a transaction.
   It creates server-only payment/token functions and enforces owner-scoped reads.
3. Existing legacy subscription metadata is copied into a protected snapshot.
   Once Google verifies a profile, a valid, unexpired matching plan is carried over.
   Malformed/expired entries remain preserved for review. This preserves the current
   entitlement state; it is not an audit of historical payment authenticity.
4. The migration preserves the two admin email identities already in the original
   backend by assigning `app_metadata.role = admin` to confirmed matching accounts.
   Operators should sign out/in after migration to refresh their frontend session.
5. Configure Render environment variables below, then deploy this backend revision.
   Startup validates required configuration and checks that the new ledger exists.
6. Configure the frontend variables below and deploy its matching revision.
7. Sign in, reconnect/load Google locations, verify the shared token balance and
   profile plan, then resume authenticated cron jobs. Reconnecting is required to
   populate verified account/location ownership; cached browser metadata is not trusted.

To initialize existing accounts without waiting for each user to log in, run
`python scripts/bootstrap_locations.py` first to see the connection count, then
`python scripts/bootstrap_locations.py --apply` **after the SQL migration**. This
verifies Google locations using saved refresh tokens and registers them through the
same backend flow. Accounts with expired/revoked connections must sign in again.

## Render configuration

Keep existing secret values in Render's environment settings; do not commit `.env.local`.
Local development loads `.env` and then `.env.local`, without overriding actual environment variables.

Required at startup:

- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
- `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
- `GROQ_API_KEY`

Additional settings:

- `ALLOWED_ORIGINS=https://www.gbpautomaster.in,https://gbpautomaster.in`
- `CRON_SECRET`: generate a long random secret, e.g. with Python's
  `secrets.token_urlsafe(32)`. Store the same value in the cron request header.
- `BUSINESS_TIMEZONE=Asia/Kolkata`
- `GOOGLE_MAPS_API_KEY` enables real competitor data; reports fail explicitly without it.
- `SERPAPI_KEY` is used by the administrator's competitor scan.
- Optional `PROMO_CODE`: leave unset to disable promos. Each configured code can be
  redeemed once per account, across products. The old source-code promo is disabled.

For Google Pub/Sub push, configure authenticated delivery using the exact service
account in `GOOGLE_PUBSUB_SERVICE_ACCOUNT`, and set `GOOGLE_PUBSUB_AUDIENCE` to the
push endpoint URL. Unauthenticated webhook deliveries are rejected.

## Cron configuration

Every `/api/cron/*` request must include:

```text
Authorization: Bearer <the CRON_SECRET value>
```

Keep the existing schedule for these routes:

- `/api/cron/reply-reviews`
- `/api/cron/daily-backlog-reviews`
- `/api/cron/publish-scheduled`
- `/api/cron/scrub-calendar`

Do not put the secret in a URL query string. Overlapping runs return HTTP 409.
`job_runs` records completion/failure. Monthly plan credits are applied lazily when
balances or profiles are read, including by review jobs; no additional cron is needed.

## Frontend configuration

- `VITE_SUPABASE_URL`
- `VITE_SUPABASE_ANON_KEY` (public browser key, never the service-role key)
- `VITE_API_URL=https://gbp-auto-master-backend-us.onrender.com`

`npm run build` now runs TypeScript before Vite. Tailwind is compiled through its
Vite plugin, screens load separately, and jsPDF was updated to clear npm advisories.
The old `Dashboard.tsx` and `Dashboard.legacy.tsx` are retained as inactive reference
implementations and excluded from the active TypeScript build. `App.tsx` uses V2.

## Verification

```text
python -m pip install -r requirements-dev.txt
python -m pytest -q
cd tests/sql
npm ci
npm test
```

Run `npm run build`, `npm run lint`, and `npm audit` in the UI repository. The SQL
suite applies the migration to a local PostgreSQL runtime (PGlite) with a minimal
Supabase-shaped fixture. It does not modify the live database.

After rollout, verify Google OAuth, first location registration, a Razorpay **test-mode**
purchase/top-up and duplicate verification, account balance sharing across profiles,
calendar scheduling/publishing, and authenticated cron execution. Do not use live
payment or review-posting tests without an appropriate test account.

## Recovery and known operational limits

- An uncertain Google write (for example a timeout after transmission) keeps its
  token operation reserved or its calendar post `publishing`. Inspect Google first.
  If the reply exists, complete the operation; if confirmed absent, refund via
  `finish_tokens(operation_id, false)` from the SQL editor before retrying. Never
  blindly refund/retry an ambiguous delivery.
- A worker crash can leave `job_runs.status='running'`. Confirm no worker is active,
  reconcile pending writes, then mark that job `failed` before running it again.
- Declined generation/review publication is refunded. Calendar's 5-token scheduling
  charge is not refunded by deleting the scheduled post.
- Razorpay verification requires a captured payment. Enable/check capture configuration.
  A lost checkout callback may require support to re-run verification for the stored
  order; there is not yet a Razorpay webhook reconciliation service.
- Old pending payments created before cutover have no `billing_orders` record. Finish
  or reconcile them before rollout; the new verifier will not trust an unknown order.
- Legacy database constraints/policies outside the exposed schema still need validation
  during SQL-editor rollout. The browser deployment tool was unavailable in this session,
  so live RLS, Google, payment, and cron smoke tests have not been run.
