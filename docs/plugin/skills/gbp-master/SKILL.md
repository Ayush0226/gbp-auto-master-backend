---
name: gbp-master
description: Manage connected Google Business Profile accounts, locations, reviews, reply automation, approval queues, campaigns, media, and measured local rank reports.
---

# GBP Master

Use the authenticated GBP Master MCP server for Google Business Profile work.

1. Resolve the intended business with `get_account_overview`, `list_business_accounts` or `list_locations`.
2. Authentication supplies user identity and ownership. Never request or accept provider secrets, a Supabase user ID, or another user's identifiers as authorization.
3. GBP Master has no usage-credit balance, action charges or top-up system. Payments are temporarily paused. A lifetime launch entitlement covers current supported features; operational safeguards and external-provider limits still apply.
4. Read automation rules before editing them. Explain the affected location or account default, rating range, mode, delay, daily limit, tone and instructions. Obtain confirmation before enabling future public replies.
5. Before publishing or approving a reply, show the exact review, reply text and publication time, and obtain explicit confirmation.
6. Resolve media through `list_media_assets`. Before scheduling a campaign, show the complete content, post type, every target location, media and zoned publication time. Obtain explicit confirmation.
7. Identify the exact post, reply or campaign before cancellation. A delivery already in progress may require reconciliation; never claim it was cancelled unless the server confirms it.
8. Photos require an owned, validated, durable HTTPS media asset. Video publication is unavailable.
9. A missing or expired Google connection requires reconnection at https://gbpautomaster.in. Never reveal refresh credentials.
10. An uncertain delivery must be inspected in Google before retrying. Never create a replacement request ID or reset a held delivery to bypass duplicate protection.
11. Rank reports measure one keyword against up to eleven local results. Explain the location, keyword and search scope, obtain confirmation, and reuse the same request ID for retries. A null actual rank means not found in the measured results. Present the PDF link and its expiry.
12. Report server errors and partial results honestly. Do not invent metrics, ranks or successful publications.
