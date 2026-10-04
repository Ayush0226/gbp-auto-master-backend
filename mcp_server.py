"""Authenticated MCP server for GBP Auto Master."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Literal
from urllib.parse import urlparse

import jwt
from http_client import requests
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from starlette.concurrency import run_in_threadpool

from mcp_auth import DEFAULT_MCP_RESOURCE_URL, OAUTH_SCOPES


SERVER_INSTRUCTIONS = (
    "Use GBP Master to manage the authenticated user's Google Business Profile accounts, "
    "locations, reviews, review automation, content campaigns, scheduled posts and shared "
    "credits. Resolve locations before location-scoped actions. Show the exact public text, "
    "targets, time and credit charge before a write that publishes or schedules content. "
    "Never ask for or return Google access tokens, refresh tokens or Supabase user IDs."
)


class LocationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_id: str
    name: str
    account_id: str | None = None
    plan: str
    subscription_expires_at: datetime | None


class LocationList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locations: list[LocationSummary]


class ActionCosts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publish_review_reply: float = 2.5
    schedule_post: float = 5.0


class CreditBalance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    balance: float
    action_costs: ActionCosts


class ReviewSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str
    reviewer_name: str
    rating: int = Field(ge=1, le=5)
    comment: str
    created_at: datetime | None
    has_reply: bool
    reply_text: str | None


class ReviewList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_id: str
    reviews: list[ReviewSummary]
    next_page_token: str | None
    total_review_count: int = Field(ge=0)
    average_rating: float = Field(ge=0, le=5)


class ReviewDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_id: str
    review_id: str
    reviewer_name: str
    rating: int = Field(ge=1, le=5)
    review_comment: str
    draft_text: str
    publish_cost: float = 2.5


class PublishedReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["published"] = "published"
    location_id: str
    review_id: str
    published_reply: str
    credits_charged: float = 2.5
    remaining_balance: float


class ScheduledPostSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post_id: str
    location_id: str
    scheduled_for: datetime
    caption: str
    media_url: str | None
    post_type: Literal["LOCAL_POST", "PHOTO", "VIDEO"]
    status: Literal["scheduled", "publishing", "published", "failed"]


class ScheduledPostList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    posts: list[ScheduledPostSummary]


class ScheduledPostResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["scheduled"] = "scheduled"
    post_id: str
    location_id: str
    scheduled_for: datetime
    credits_charged: float = 5.0
    remaining_balance: float


class CancelledPost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["cancelled"] = "cancelled"
    post_id: str
    credits_refunded: float = 0.0


class BusinessAccountSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    name: str
    location_count: int = Field(ge=0)


class BusinessAccountList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[BusinessAccountSummary]


class AccountOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: str
    subscription_status: str
    subscription_expires_at: datetime | None
    credit_balance: float
    google_account_count: int = Field(ge=0)
    location_count: int = Field(ge=0)
    automation_rule_count: int = Field(ge=0)
    scheduled_campaign_count: int = Field(ge=0)


class AutomationRuleSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    name: str
    location_id: str | None
    enabled: bool
    mode: Literal["draft", "approval", "auto_publish"]
    min_rating: int = Field(ge=1, le=5)
    max_rating: int = Field(ge=1, le=5)
    tone: str
    language: str
    delay_minutes: int = Field(ge=0)
    daily_limit: int = Field(ge=1)


class AutomationRuleList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[AutomationRuleSummary]


class AutomationRuleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["saved"] = "saved"
    rule: AutomationRuleSummary


class CampaignSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    title: str
    topic_type: Literal["STANDARD", "EVENT", "OFFER"]
    summary: str
    timezone: str
    status: str
    scheduled_for: datetime | None
    location_ids: list[str]


class CampaignList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaigns: list[CampaignSummary]


class CampaignDraftResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["draft"] = "draft"
    campaign_id: str
    location_count: int = Field(ge=1)
    scheduling_cost: float = Field(ge=0)


class CampaignScheduleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["scheduled"] = "scheduled"
    campaign_id: str
    location_count: int = Field(ge=1)
    scheduled_for: datetime
    credits_charged: float = Field(ge=0)
    remaining_balance: float = Field(ge=0)


class CampaignCancelResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["cancelled"] = "cancelled"
    campaign_id: str
    credits_refunded: float = 0


class ReviewReplyJobSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    location_id: str
    review_id: str
    rating: int = Field(ge=1, le=5)
    review_text: str
    draft_text: str
    status: Literal["draft", "pending_approval", "scheduled", "publishing", "published", "failed", "cancelled"]
    scheduled_for: datetime | None
    last_error: str | None
    credits_charged: float = Field(ge=0)


class ReviewReplyJobList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jobs: list[ReviewReplyJobSummary]


class ReviewReplyApprovalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["scheduled"] = "scheduled"
    job_id: str
    scheduled_for: datetime
    credits_due: float = 2.5


class ReviewReplyCancellationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["cancelled"] = "cancelled"
    job_id: str
    credits_refunded: float = 0


class MediaAssetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    media_kind: Literal["photo", "video"]
    mime_type: str
    public_url: str | None
    status: str
    created_at: datetime | None


class MediaAssetList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assets: list[MediaAssetSummary]


STAR_RATINGS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


def _scopes(claims: dict[str, Any]) -> list[str]:
    raw = claims.get("scope") or claims.get("scopes")
    if isinstance(raw, str):
        scopes = raw.split()
    elif isinstance(raw, list):
        scopes = [scope for scope in raw if isinstance(scope, str)]
    else:
        scopes = []
    # Supabase validates the account token. Some Supabase sessions do not repeat
    # the OAuth grant scopes in the JWT, so use the server's supported identity
    # scopes after successful validation.
    return scopes or list(OAUTH_SCOPES)


class SupabaseTokenVerifier(TokenVerifier):
    """Validate MCP bearer tokens with Supabase Auth and expose the account id."""

    def __init__(self, db: Any):
        self.db = db

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or self.db is None:
            return None
        try:
            result = await run_in_threadpool(self.db.auth.get_user, token)
            user = result.user
            if not user:
                return None
            claims = jwt.decode(
                token,
                options={"verify_signature": False, "verify_exp": False, "verify_aud": False},
            )
        except Exception:
            return None

        client_id = claims.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            # Supabase OAuth Server access tokens include client_id. Reject ordinary
            # browser sessions so only a user-approved OAuth application can call MCP.
            return None
        expires_at = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=_scopes(claims),
            expires_at=int(expires_at) if isinstance(expires_at, (int, float)) else None,
            subject=str(user.id),
            claims={"email": getattr(user, "email", None)},
        )


class GBPReadService:
    """Account-scoped reads used by MCP tools."""

    def __init__(
        self,
        db: Any,
        google_token_getter: Callable[[str], str] | None = None,
        ai_reply_generator: Callable[[str], str] | None = None,
    ):
        self.db = db
        self.google_token_getter = google_token_getter
        self.ai_reply_generator = ai_reply_generator

    async def list_locations(self, user_id: str) -> LocationList:
        rows = (
            await run_in_threadpool(
                lambda: self.db.table("location_profiles")
                .select("location_id,account_id,plan_type,subscription_end")
                .eq("user_id", user_id)
                .execute()
            )
        ).data or []

        auth_user = await run_in_threadpool(self.db.auth.admin.get_user_by_id, user_id)
        metadata = getattr(auth_user.user, "user_metadata", None) or {}
        cached = metadata.get("cached_locations") or []
        names = {
            item.get("id"): item.get("name")
            for item in cached
            if isinstance(item, dict) and item.get("id")
        }
        subscription_rows = (
            await run_in_threadpool(
                lambda: self.db.table("account_subscriptions")
                .select("plan_type,status,expires_at")
                .eq("user_id", user_id)
                .execute()
            )
        ).data or []
        account_subscription = subscription_rows[0] if subscription_rows and "status" in subscription_rows[0] else None
        now = datetime.now(timezone.utc)
        locations: list[LocationSummary] = []
        for row in rows:
            expires_at = _parse_datetime((account_subscription or {}).get("expires_at") or row.get("subscription_end"))
            plan = (account_subscription or {}).get("plan_type") or row.get("plan_type") or "free"
            if account_subscription and account_subscription.get("status") != "active":
                plan = "free"
            if plan != "free" and (expires_at is None or expires_at <= now):
                plan = "free"
            location_id = row["location_id"]
            locations.append(
                LocationSummary(
                    location_id=location_id,
                    name=names.get(location_id) or "Business",
                    account_id=row.get("account_id"),
                    plan=plan,
                    subscription_expires_at=expires_at,
                )
            )
        return LocationList(locations=locations)

    async def get_credit_balance(self, user_id: str) -> CreditBalance:
        location_rows = (
            await run_in_threadpool(
                lambda: self.db.table("location_profiles")
                .select("location_id")
                .eq("user_id", user_id)
                .execute()
            )
        ).data or []
        for row in location_rows[:1]:
            await run_in_threadpool(
                lambda location_id=row["location_id"]: self.db.rpc(
                    "refresh_monthly_tokens",
                    {"p_location": location_id, "p_user": user_id},
                ).execute()
            )
        account = (
            await run_in_threadpool(
                lambda: self.db.rpc("ensure_account", {"p_user": user_id}).execute()
            )
        ).data or {}
        return CreditBalance(
            balance=max(0.0, float(account.get("tokens_balance") or 0)),
            action_costs=ActionCosts(),
        )

    async def list_business_accounts(self, user_id: str) -> BusinessAccountList:
        accounts = (
            await run_in_threadpool(
                lambda: self.db.table("gbp_accounts")
                .select("resource_name,display_name")
                .eq("user_id", user_id)
                .order("display_name")
                .execute()
            )
        ).data or []
        locations = (
            await run_in_threadpool(
                lambda: self.db.table("location_profiles")
                .select("account_id")
                .eq("user_id", user_id)
                .execute()
            )
        ).data or []
        counts: dict[str, int] = {}
        for location in locations:
            account_id = location.get("account_id")
            if account_id:
                counts[account_id] = counts.get(account_id, 0) + 1
        return BusinessAccountList(accounts=[
            BusinessAccountSummary(
                account_id=row["resource_name"],
                name=row.get("display_name") or "Google Business Profile account",
                location_count=counts.get(row["resource_name"], 0),
            ) for row in accounts
        ])

    async def get_account_overview(self, user_id: str) -> AccountOverview:
        balance = await self.get_credit_balance(user_id)
        subscription_rows, accounts, locations, rules, campaigns = await run_in_threadpool(
            lambda: (
                self.db.table("account_subscriptions").select("plan_type,status,expires_at").eq("user_id", user_id).execute().data or [],
                self.db.table("gbp_accounts").select("resource_name").eq("user_id", user_id).execute().data or [],
                self.db.table("location_profiles").select("location_id").eq("user_id", user_id).execute().data or [],
                self.db.table("review_automation_rules").select("id").eq("user_id", user_id).execute().data or [],
                self.db.table("content_campaigns").select("id").eq("user_id", user_id).eq("status", "scheduled").execute().data or [],
            )
        )
        subscription = subscription_rows[0] if subscription_rows else {}
        return AccountOverview(
            plan=subscription.get("plan_type") or "free",
            subscription_status=subscription.get("status") or "active",
            subscription_expires_at=_parse_datetime(subscription.get("expires_at")),
            credit_balance=balance.balance,
            google_account_count=len(accounts),
            location_count=len(locations),
            automation_rule_count=len(rules),
            scheduled_campaign_count=len(campaigns),
        )

    async def list_automation_rules(self, user_id: str) -> AutomationRuleList:
        rows = (
            await run_in_threadpool(
                lambda: self.db.table("review_automation_rules")
                .select("id,name,location_id,enabled,mode,min_rating,max_rating,tone,language,delay_minutes,daily_limit")
                .eq("user_id", user_id)
                .order("created_at")
                .execute()
            )
        ).data or []
        return AutomationRuleList(rules=[self._automation_rule(row) for row in rows])

    async def configure_automation_rule(
        self,
        user_id: str,
        name: str,
        location_id: str | None,
        enabled: bool,
        mode: Literal["draft", "approval", "auto_publish"],
        min_rating: int,
        max_rating: int,
        tone: str,
        language: str,
        delay_minutes: int,
        daily_limit: int,
        custom_instructions: str,
        blocked_terms: list[str],
    ) -> AutomationRuleResult:
        if min_rating > max_rating:
            raise ValueError("Minimum rating cannot exceed maximum rating")
        normalized_location = None
        if location_id:
            normalized_location = (await self._owned_location(user_id, location_id))["location_id"]
        row = {
            "user_id": user_id,
            "location_id": normalized_location,
            "name": name.strip(),
            "enabled": enabled,
            "mode": mode,
            "min_rating": min_rating,
            "max_rating": max_rating,
            "tone": tone.strip(),
            "language": language.strip(),
            "delay_minutes": delay_minutes,
            "daily_limit": daily_limit,
            "custom_instructions": custom_instructions.strip(),
            "blocked_terms": list(dict.fromkeys(term.strip() for term in blocked_terms if term.strip())),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        query = self.db.table("review_automation_rules").select("id").eq("user_id", user_id)
        query = query.eq("location_id", normalized_location) if normalized_location else query.is_("location_id", "null")
        existing = (await run_in_threadpool(query.execute)).data or []
        if existing:
            saved = await run_in_threadpool(
                lambda: self.db.table("review_automation_rules").update(row).eq("id", existing[0]["id"]).eq("user_id", user_id).execute()
            )
        else:
            saved = await run_in_threadpool(lambda: self.db.table("review_automation_rules").insert(row).execute())
        return AutomationRuleResult(rule=self._automation_rule(saved.data[0]))

    async def list_content_campaigns(
        self, user_id: str, status: str, limit: int
    ) -> CampaignList:
        query = self.db.table("content_campaigns").select("id,title,topic_type,summary,timezone,status,scheduled_for").eq("user_id", user_id)
        if status != "all":
            query = query.eq("status", status)
        campaigns = (
            await run_in_threadpool(lambda: query.order("created_at", desc=True).limit(limit).execute())
        ).data or []
        ids = [row["id"] for row in campaigns]
        deliveries = []
        if ids:
            deliveries = (
                await run_in_threadpool(
                    lambda: self.db.table("campaign_locations").select("campaign_id,location_id").eq("user_id", user_id).in_("campaign_id", ids).execute()
                )
            ).data or []
        locations: dict[str, list[str]] = {}
        for delivery in deliveries:
            locations.setdefault(str(delivery["campaign_id"]), []).append(delivery["location_id"])
        return CampaignList(campaigns=[CampaignSummary(
            campaign_id=str(row["id"]), title=row["title"], topic_type=row["topic_type"],
            summary=row.get("summary") or "", timezone=row.get("timezone") or "Asia/Kolkata",
            status=row["status"], scheduled_for=_parse_datetime(row.get("scheduled_for")),
            location_ids=locations.get(str(row["id"]), []),
        ) for row in campaigns])

    async def create_content_campaign(
        self, user_id: str, title: str, topic_type: Literal["STANDARD", "EVENT", "OFFER"],
        summary: str, timezone_name: str, location_ids: list[str],
        call_to_action: dict[str, Any] | None, event_details: dict[str, Any] | None,
        offer_details: dict[str, Any] | None, media_asset_id: str | None,
    ) -> CampaignDraftResult:
        normalized: list[str] = []
        for location_id in dict.fromkeys(location_ids):
            normalized.append((await self._owned_location(user_id, location_id))["location_id"])
        if not normalized:
            raise ValueError("Select at least one location")
        result = (
            await run_in_threadpool(
                lambda: self.db.rpc("create_content_campaign", {
                    "p_user": user_id, "p_title": title.strip(), "p_topic_type": topic_type,
                    "p_summary": summary.strip(), "p_timezone": timezone_name,
                    "p_location_ids": normalized, "p_call_to_action": call_to_action,
                    "p_event_details": event_details, "p_offer_details": offer_details,
                    "p_media_asset": media_asset_id,
                }).execute()
            )
        ).data or {}
        return CampaignDraftResult(
            campaign_id=str(result["campaign_id"]), location_count=int(result["location_count"]),
            scheduling_cost=int(result["location_count"]) * 5,
        )

    async def schedule_content_campaign(
        self, user_id: str, campaign_id: str, publish_at: datetime
    ) -> CampaignScheduleResult:
        scheduled_for = _as_utc(publish_at)
        result = (
            await run_in_threadpool(
                lambda: self.db.rpc("schedule_content_campaign", {
                    "p_user": user_id, "p_campaign": campaign_id,
                    "p_publish_at": scheduled_for.isoformat(),
                }).execute()
            )
        ).data or {}
        return CampaignScheduleResult(
            campaign_id=str(result["campaign_id"]), location_count=int(result["location_count"]),
            scheduled_for=scheduled_for, credits_charged=float(result["credits_charged"]),
            remaining_balance=float(result.get("balance") or 0),
        )

    async def cancel_content_campaign(self, user_id: str, campaign_id: str) -> CampaignCancelResult:
        result = (
            await run_in_threadpool(
                lambda: self.db.rpc("cancel_content_campaign", {"p_user": user_id, "p_campaign": campaign_id}).execute()
            )
        ).data or {}
        return CampaignCancelResult(campaign_id=str(result["campaign_id"]))

    async def list_review_reply_jobs(
        self, user_id: str, status: str, limit: int
    ) -> ReviewReplyJobList:
        query = self.db.table("review_reply_jobs").select(
            "id,location_id,review_name,rating,review_text,draft_text,status,scheduled_for,last_error,credits_charged"
        ).eq("user_id", user_id)
        if status != "all":
            query = query.eq("status", status)
        rows = (
            await run_in_threadpool(lambda: query.order("created_at", desc=True).limit(limit).execute())
        ).data or []
        return ReviewReplyJobList(jobs=[self._review_reply_job(row) for row in rows])

    async def approve_review_reply_job(
        self, user_id: str, job_id: str, publish_at: datetime | None
    ) -> ReviewReplyApprovalResult:
        rows = (
            await run_in_threadpool(
                lambda: self.db.table("review_reply_jobs").select("id,status")
                .eq("id", job_id).eq("user_id", user_id).limit(1).execute()
            )
        ).data or []
        if not rows:
            raise ValueError("Review reply job not found")
        current = rows[0]["status"]
        if current not in {"draft", "pending_approval", "failed"}:
            raise ValueError(f"A {current} reply cannot be approved")
        scheduled_for = _as_utc(publish_at) if publish_at else datetime.now(timezone.utc)
        saved = (
            await run_in_threadpool(
                lambda: self.db.table("review_reply_jobs").update({
                    "status": "scheduled", "scheduled_for": scheduled_for.isoformat(),
                    "last_error": None, "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", job_id).eq("user_id", user_id).eq("status", current).execute()
            )
        ).data or []
        if not saved:
            raise RuntimeError("The reply changed while it was being approved")
        return ReviewReplyApprovalResult(job_id=job_id, scheduled_for=scheduled_for)

    async def cancel_review_reply_job(
        self, user_id: str, job_id: str
    ) -> ReviewReplyCancellationResult:
        rows = (
            await run_in_threadpool(
                lambda: self.db.table("review_reply_jobs").select("id,status")
                .eq("id", job_id).eq("user_id", user_id).limit(1).execute()
            )
        ).data or []
        if not rows:
            raise ValueError("Review reply job not found")
        current = rows[0]["status"]
        if current not in {"draft", "pending_approval", "scheduled", "failed"}:
            raise ValueError(f"A {current} reply cannot be cancelled")
        saved = (
            await run_in_threadpool(
                lambda: self.db.table("review_reply_jobs").update({
                    "status": "cancelled", "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", job_id).eq("user_id", user_id).eq("status", current).execute()
            )
        ).data or []
        if not saved:
            raise RuntimeError("The reply changed while it was being cancelled")
        return ReviewReplyCancellationResult(job_id=job_id)

    async def list_media_assets(self, user_id: str, media_kind: str, limit: int) -> MediaAssetList:
        query = self.db.table("media_assets").select(
            "id,media_kind,mime_type,public_url,status,created_at"
        ).eq("user_id", user_id).neq("status", "deleted")
        if media_kind != "all":
            query = query.eq("media_kind", media_kind)
        rows = (
            await run_in_threadpool(lambda: query.order("created_at", desc=True).limit(limit).execute())
        ).data or []
        return MediaAssetList(assets=[MediaAssetSummary(
            asset_id=str(row["id"]), media_kind=row["media_kind"], mime_type=row["mime_type"],
            public_url=row.get("public_url"), status=row.get("status") or "uploaded",
            created_at=_parse_datetime(row.get("created_at")),
        ) for row in rows])

    async def list_reviews(
        self,
        user_id: str,
        location_id: str,
        reply_status: Literal["all", "unanswered", "answered"],
        limit: int,
        page_token: str | None,
    ) -> ReviewList:
        profile = await self._owned_location(user_id, location_id)
        access_token = await self._google_access_token(user_id)
        params: dict[str, Any] = {"pageSize": limit, "orderBy": "updateTime desc"}
        if page_token:
            params["pageToken"] = page_token
        response = await run_in_threadpool(
            lambda: requests.get(
                self._reviews_url(profile),
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
        )
        if not response.ok:
            raise RuntimeError("Google review lookup failed; reconnect Google and retry")
        payload = response.json()
        reviews = [self._review_summary(item) for item in payload.get("reviews", [])]
        if reply_status == "unanswered":
            reviews = [review for review in reviews if not review.has_reply]
        elif reply_status == "answered":
            reviews = [review for review in reviews if review.has_reply]
        return ReviewList(
            location_id=profile["location_id"],
            reviews=reviews,
            next_page_token=payload.get("nextPageToken"),
            total_review_count=int(payload.get("totalReviewCount") or len(reviews)),
            average_rating=float(payload.get("averageRating") or 0),
        )

    async def draft_review_reply(
        self, user_id: str, location_id: str, review_id: str
    ) -> ReviewDraft:
        profile = await self._owned_location(user_id, location_id)
        review_name = self._review_name(profile, review_id)
        access_token = await self._google_access_token(user_id)
        review = await self._fetch_review(review_name, access_token)
        if review.get("reviewReply"):
            raise ValueError("This review already has a public reply")
        settings = await self._ai_settings(user_id, profile["location_id"])
        summary = self._review_summary(review)
        prompt = self._reply_prompt(summary, settings)
        if self.ai_reply_generator is None:
            raise RuntimeError("AI reply generation is not configured")
        draft = await run_in_threadpool(self.ai_reply_generator, prompt)
        if not isinstance(draft, str) or not draft.strip():
            raise RuntimeError("AI returned an empty review reply")
        return ReviewDraft(
            location_id=profile["location_id"],
            review_id=review_name,
            reviewer_name=summary.reviewer_name,
            rating=summary.rating,
            review_comment=summary.comment,
            draft_text=draft.strip()[:4096],
        )

    async def publish_review_reply(
        self, user_id: str, location_id: str, review_id: str, reply_text: str
    ) -> PublishedReply:
        profile = await self._owned_location(user_id, location_id)
        review_name = self._review_name(profile, review_id)
        access_token = await self._google_access_token(user_id)
        review = await self._fetch_review(review_name, access_token)
        if review.get("reviewReply"):
            raise ValueError("This review already has a public reply")
        reply = reply_text.strip()
        if not reply:
            raise ValueError("Reply text is required")
        operation = f"reply:{review_name}"
        reservation = (
            await run_in_threadpool(
                lambda: self.db.rpc(
                    "reserve_tokens",
                    {
                        "p_operation": operation,
                        "p_location": profile["location_id"],
                        "p_user": user_id,
                        "p_amount": 2.5,
                    },
                ).execute()
            )
        ).data or {}
        if not reservation.get("success"):
            raise ValueError(reservation.get("error") or "Insufficient credits")
        try:
            response = await run_in_threadpool(
                lambda: requests.put(
                    f"https://mybusiness.googleapis.com/v4/{review_name}/reply",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"comment": reply},
                )
            )
        except Exception:
            raise RuntimeError(
                "Google delivery is uncertain; inspect the review before retrying"
            ) from None
        await run_in_threadpool(
            lambda: self.db.rpc(
                "finish_tokens", {"p_operation": operation, "p_success": response.ok}
            ).execute()
        )
        if not response.ok:
            raise RuntimeError("Google rejected the reply; reserved credits were refunded")
        return PublishedReply(
            location_id=profile["location_id"],
            review_id=review_name,
            published_reply=reply,
            remaining_balance=max(0.0, float(reservation.get("balance") or 0)),
        )

    async def list_scheduled_posts(
        self,
        user_id: str,
        location_id: str,
        status: Literal["all", "scheduled", "publishing", "published", "failed"],
        from_time: datetime | None,
        to_time: datetime | None,
        limit: int,
    ) -> ScheduledPostList:
        profile = await self._owned_location(user_id, location_id)
        query = (
            self.db.table("calendar_posts")
            .select("id,location_id,publish_at,post_date,caption,image_url,post_type,status")
            .eq("user_id", user_id)
            .eq("location_id", profile["location_id"])
        )
        if status != "all":
            query = query.eq("status", status)
        if from_time:
            query = query.gte("publish_at", _as_utc(from_time).isoformat())
        if to_time:
            query = query.lte("publish_at", _as_utc(to_time).isoformat())
        rows = (
            await run_in_threadpool(
                lambda: query.order("publish_at", desc=False).limit(limit).execute()
            )
        ).data or []
        posts = [self._scheduled_post(row) for row in rows]
        return ScheduledPostList(posts=posts)

    async def schedule_post(
        self,
        user_id: str,
        location_id: str,
        publish_at: datetime,
        caption: str,
        post_type: Literal["LOCAL_POST", "PHOTO", "VIDEO"],
        media_url: HttpUrl | None,
    ) -> ScheduledPostResult:
        profile = await self._owned_location(user_id, location_id)
        scheduled_for = _as_utc(publish_at)
        if scheduled_for <= datetime.now(timezone.utc):
            raise ValueError("publish_at must be in the future")
        clean_caption = caption.strip()
        media = str(media_url) if media_url is not None else None
        if media and not media.startswith("https://"):
            raise ValueError("Media URL must use HTTPS")
        if not clean_caption and not media:
            raise ValueError("Add a caption or media URL")
        if post_type in ("PHOTO", "VIDEO") and not media:
            raise ValueError(f"{post_type} scheduling requires a media URL")
        scheduled = (
            await run_in_threadpool(
                lambda: self.db.rpc(
                    "schedule_post_at",
                    {
                        "p_user": user_id,
                        "p_location": profile["location_id"],
                        "p_publish_at": scheduled_for.isoformat(),
                        "p_caption": clean_caption,
                        "p_image": media,
                        "p_type": post_type,
                    },
                ).execute()
            )
        ).data or {}
        if scheduled.get("status") != "success":
            raise ValueError(scheduled.get("error") or "Could not schedule the post")
        return ScheduledPostResult(
            post_id=str(scheduled["id"]),
            location_id=profile["location_id"],
            scheduled_for=scheduled_for,
            remaining_balance=float(scheduled.get("balance") or 0),
        )

    async def cancel_scheduled_post(
        self, user_id: str, location_id: str, post_id: str
    ) -> CancelledPost:
        profile = await self._owned_location(user_id, location_id)
        rows = (
            await run_in_threadpool(
                lambda: self.db.table("calendar_posts")
                .delete()
                .eq("id", post_id)
                .eq("user_id", user_id)
                .eq("location_id", profile["location_id"])
                .eq("status", "scheduled")
                .execute()
            )
        ).data or []
        if not rows:
            raise ValueError("Only a pending scheduled post can be cancelled")
        return CancelledPost(post_id=post_id)

    async def _owned_location(self, user_id: str, location_id: str) -> dict[str, Any]:
        normalized = _normalize_location(location_id)
        rows = (
            await run_in_threadpool(
                lambda: self.db.table("location_profiles")
                .select("location_id,user_id,account_id")
                .eq("location_id", normalized)
                .eq("user_id", user_id)
                .execute()
            )
        ).data or []
        if not rows:
            raise PermissionError("Reconnect Google or choose a location owned by this account")
        return rows[0]

    async def _user_metadata(self, user_id: str) -> dict[str, Any]:
        result = await run_in_threadpool(self.db.auth.admin.get_user_by_id, user_id)
        user = getattr(result, "user", None)
        if user is None:
            raise PermissionError("GBP Master account was not found")
        return getattr(user, "user_metadata", None) or {}

    async def _google_access_token(self, user_id: str) -> str:
        metadata = await self._user_metadata(user_id)
        refresh_token = metadata.get("google_refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise PermissionError("Reconnect Google Business Profile before using this tool")
        if self.google_token_getter is None:
            raise RuntimeError("Google token exchange is not configured")
        try:
            token = await run_in_threadpool(self.google_token_getter, refresh_token)
        except Exception:
            raise PermissionError("Google connection expired; reconnect Google Business Profile") from None
        if not token:
            raise PermissionError("Google connection expired; reconnect Google Business Profile")
        return token

    async def _ai_settings(self, user_id: str, location_id: str) -> dict[str, Any]:
        metadata = await self._user_metadata(user_id)
        settings = metadata.get("ai_settings") or {}
        selected = settings.get(location_id) if isinstance(settings, dict) else {}
        return selected if isinstance(selected, dict) else {}

    @staticmethod
    def _reviews_url(profile: dict[str, Any]) -> str:
        return (
            "https://mybusiness.googleapis.com/v4/"
            f"{profile['account_id']}/{profile['location_id']}/reviews"
        )

    @staticmethod
    def _review_name(profile: dict[str, Any], review_id: str) -> str:
        expected_prefix = f"{profile['account_id']}/{profile['location_id']}/reviews/"
        if re.fullmatch(r"[A-Za-z0-9_-]+", review_id):
            return expected_prefix + review_id
        if not review_id.startswith(expected_prefix) or not re.fullmatch(
            r"accounts/[A-Za-z0-9_-]+/locations/[A-Za-z0-9_-]+/reviews/[A-Za-z0-9_-]+",
            review_id,
        ):
            raise PermissionError("Review does not belong to the selected location")
        return review_id

    async def _fetch_review(self, review_name: str, access_token: str) -> dict[str, Any]:
        response = await run_in_threadpool(
            lambda: requests.get(
                f"https://mybusiness.googleapis.com/v4/{review_name}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        )
        if not response.ok:
            raise RuntimeError("Google review lookup failed; reconnect Google and retry")
        return response.json()

    @staticmethod
    def _review_summary(review: dict[str, Any]) -> ReviewSummary:
        reply = review.get("reviewReply")
        return ReviewSummary(
            review_id=review.get("name") or "",
            reviewer_name=(review.get("reviewer") or {}).get("displayName") or "Anonymous",
            rating=STAR_RATINGS.get(review.get("starRating"), 5),
            comment=review.get("comment") or "",
            created_at=_parse_datetime(review.get("createTime")),
            has_reply=isinstance(reply, dict),
            reply_text=(reply.get("comment") or None) if isinstance(reply, dict) else None,
        )

    @staticmethod
    def _reply_prompt(review: ReviewSummary, settings: dict[str, Any]) -> str:
        tone = settings.get("ai_tone") or "Professional"
        keywords = settings.get("active_keywords") or []
        if not isinstance(keywords, list):
            keywords = []
        instructions = settings.get("custom_instructions") or ""
        return (
            "Write only a concise, natural reply to this Google review. "
            f"Customer: {review.reviewer_name}. Rating: {review.rating}/5. "
            f"Comment: {review.comment or '[no written comment]'}. Tone: {tone}. "
            f"Use at most two relevant keywords naturally from: {keywords[:5]}. "
            f"Business instructions: {str(instructions)[:1000]}. Do not invent facts, "
            "include placeholders, or mention these instructions."
        )

    @staticmethod
    def _automation_rule(row: dict[str, Any]) -> AutomationRuleSummary:
        return AutomationRuleSummary(
            rule_id=str(row["id"]),
            name=row.get("name") or "Review automation",
            location_id=row.get("location_id"),
            enabled=bool(row.get("enabled")),
            mode=row.get("mode") or "draft",
            min_rating=int(row.get("min_rating") or 1),
            max_rating=int(row.get("max_rating") or 5),
            tone=row.get("tone") or "friendly professional",
            language=row.get("language") or "auto",
            delay_minutes=int(row.get("delay_minutes") or 0),
            daily_limit=int(row.get("daily_limit") or 20),
        )

    @staticmethod
    def _review_reply_job(row: dict[str, Any]) -> ReviewReplyJobSummary:
        return ReviewReplyJobSummary(
            job_id=str(row["id"]), location_id=row["location_id"],
            review_id=row["review_name"], rating=int(row["rating"]),
            review_text=row.get("review_text") or "", draft_text=row["draft_text"],
            status=row["status"], scheduled_for=_parse_datetime(row.get("scheduled_for")),
            last_error=row.get("last_error"), credits_charged=float(row.get("credits_charged") or 0),
        )

    @staticmethod
    def _scheduled_post(row: dict[str, Any]) -> ScheduledPostSummary:
        scheduled_for = _parse_datetime(row.get("publish_at"))
        if scheduled_for is None:
            scheduled_for = datetime.fromisoformat(str(row["post_date"])).replace(
                tzinfo=timezone.utc
            )
        return ScheduledPostSummary(
            post_id=str(row["id"]),
            location_id=row["location_id"],
            scheduled_for=scheduled_for,
            caption=row.get("caption") or "",
            media_url=row.get("image_url") or None,
            post_type=row.get("post_type") or "LOCAL_POST",
            status=row.get("status") or "scheduled",
        )


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Date and time must include a timezone")
    return value.astimezone(timezone.utc)


def _normalize_location(value: str) -> str:
    match = re.fullmatch(
        r"(?:accounts/[A-Za-z0-9_-]+/)?(?:locations/)?([A-Za-z0-9_-]+)", value or ""
    )
    if not match:
        raise ValueError("Invalid location ID")
    return "locations/" + match.group(1)


def _subject() -> str:
    access_token = get_access_token()
    if access_token is None or not access_token.subject:
        raise PermissionError("Authentication required")
    return access_token.subject


def create_gbp_mcp_server(
    db: Any,
    google_token_getter: Callable[[str], str] | None = None,
    ai_reply_generator: Callable[[str], str] | None = None,
) -> MCPServer:
    # Startup validation in main.py rejects missing production settings. These
    # import-safe defaults let tooling and unit tests inspect the ASGI app without
    # requiring deployment secrets.
    issuer_url = os.getenv("SUPABASE_URL", "https://project.supabase.co").rstrip("/") + "/auth/v1"
    resource_url = os.getenv("MCP_RESOURCE_URL", DEFAULT_MCP_RESOURCE_URL).rstrip("/")
    documentation_url = os.getenv(
        "MCP_DOCUMENTATION_URL", "https://gbpautomaster.in/privacy"
    )
    read_service = GBPReadService(db, google_token_getter, ai_reply_generator)
    server = MCPServer(
        name="gbp-master",
        title="GBP Master",
        description="Manage Google Business Profile reviews, credits, and scheduled content.",
        instructions=SERVER_INSTRUCTIONS,
        website_url="https://gbpautomaster.in",
        version="1.1.0",
        token_verifier=SupabaseTokenVerifier(db),
        auth=AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=resource_url,
            service_documentation_url=documentation_url,
            required_scopes=list(OAUTH_SCOPES),
            validate_token_resource=False,
        ),
    )

    @server.tool(
        name="list_locations",
        title="List business locations",
        description=(
            "List Google Business Profile locations owned by the authenticated GBP "
            "Master account. Use this before a location-scoped tool."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def list_locations() -> LocationList:
        return await read_service.list_locations(_subject())

    @server.tool(
        name="get_credit_balance",
        title="Get credit balance",
        description=(
            "Return the authenticated account's shared GBP Master credit balance and "
            "the credit cost of supported write actions."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def get_credit_balance() -> CreditBalance:
        return await read_service.get_credit_balance(_subject())

    @server.tool(
        name="list_business_accounts",
        title="List Google Business Profile accounts",
        description="List the authenticated user's Google Business Profile accounts and location counts.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    async def list_business_accounts() -> BusinessAccountList:
        return await read_service.list_business_accounts(_subject())

    @server.tool(
        name="get_account_overview",
        title="Get GBP Master account overview",
        description="Return the account plan, shared credits, connected account and location totals, review automation count, and scheduled campaign count.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    async def get_account_overview() -> AccountOverview:
        return await read_service.get_account_overview(_subject())

    @server.tool(
        name="list_review_automation_rules",
        title="List automatic review reply rules",
        description="List account-default and location-specific review reply automation rules. This read-only action costs no credits.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    async def list_review_automation_rules() -> AutomationRuleList:
        return await read_service.list_automation_rules(_subject())

    @server.tool(
        name="configure_review_automation_rule",
        title="Configure an automatic review reply rule",
        description=(
            "Create or replace the account-default rule or one location override. Enabling "
            "auto_publish authorizes future public Google replies, so call only after the user "
            "confirms the target, rating range, instructions, limits and publishing mode."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def configure_review_automation_rule(
        name: str = Field(min_length=1, max_length=100),
        location_id: str | None = None,
        enabled: bool = False,
        mode: Literal["draft", "approval", "auto_publish"] = "draft",
        min_rating: int = Field(default=4, ge=1, le=5),
        max_rating: int = Field(default=5, ge=1, le=5),
        tone: str = Field(default="friendly professional", min_length=1, max_length=100),
        language: str = Field(default="auto", min_length=2, max_length=20),
        delay_minutes: int = Field(default=0, ge=0, le=10080),
        daily_limit: int = Field(default=20, ge=1, le=500),
        custom_instructions: str = Field(default="", max_length=2000),
        blocked_terms: list[str] = Field(default_factory=list, max_length=100),
    ) -> AutomationRuleResult:
        return await read_service.configure_automation_rule(
            _subject(), name, location_id, enabled, mode, min_rating, max_rating,
            tone, language, delay_minutes, daily_limit, custom_instructions, blocked_terms,
        )

    @server.tool(
        name="list_content_campaigns",
        title="List content calendar campaigns",
        description="List account-level campaigns and every targeted GBP location. This read-only action costs no credits.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    async def list_content_campaigns(
        status: Literal["all", "draft", "awaiting_approval", "scheduled", "processing", "partially_published", "published", "failed", "cancelled"] = "all",
        limit: int = Field(default=50, ge=1, le=200),
    ) -> CampaignList:
        return await read_service.list_content_campaigns(_subject(), status, limit)

    @server.tool(
        name="create_content_campaign",
        title="Create a multi-location content draft",
        description=(
            "Create a free draft for one or more owned locations. This does not schedule, "
            "publish or spend credits. EVENT and OFFER drafts require an event title and times."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    )
    async def create_content_campaign(
        title: str = Field(min_length=1, max_length=160),
        topic_type: Literal["STANDARD", "EVENT", "OFFER"] = "STANDARD",
        summary: str = Field(default="", max_length=1500),
        location_ids: list[str] = Field(min_length=1, max_length=100),
        timezone_name: str = Field(default="Asia/Kolkata", min_length=1, max_length=100),
        call_to_action_type: Literal["BOOK", "ORDER", "SHOP", "LEARN_MORE", "SIGN_UP", "CALL"] | None = None,
        call_to_action_url: HttpUrl | None = None,
        event_title: str | None = Field(default=None, max_length=200),
        event_start: datetime | None = None,
        event_end: datetime | None = None,
        coupon_code: str | None = Field(default=None, max_length=100),
        offer_terms: str | None = Field(default=None, max_length=1000),
        media_asset_id: str | None = Field(default=None, max_length=100),
    ) -> CampaignDraftResult:
        if topic_type in ("EVENT", "OFFER") and (not event_title or not event_start or not event_end):
            raise ValueError("Event and offer campaigns require a title, start and end")
        if event_start and event_end and _as_utc(event_end) <= _as_utc(event_start):
            raise ValueError("Event end must be after event start")
        call_to_action = None
        if call_to_action_type:
            call_to_action = {"action_type": call_to_action_type, "url": str(call_to_action_url) if call_to_action_url else None}
        event_details = None if topic_type == "STANDARD" else {
            "title": event_title, "start_time": _as_utc(event_start).isoformat(),
            "end_time": _as_utc(event_end).isoformat(),
        }
        offer_details = None if topic_type != "OFFER" else {
            "coupon_code": coupon_code or "", "terms_conditions": offer_terms or "",
        }
        return await read_service.create_content_campaign(
            _subject(), title, topic_type, summary, timezone_name, location_ids,
            call_to_action, event_details, offer_details, media_asset_id,
        )

    @server.tool(
        name="schedule_content_campaign",
        title="Schedule a multi-location content campaign",
        description=(
            "Schedule an existing approved draft. The charge is 5 credits per target location. "
            "Call only after showing the complete campaign, every location, exact time and total charge."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def schedule_content_campaign(
        campaign_id: str = Field(min_length=36, max_length=36),
        publish_at: datetime = Field(),
    ) -> CampaignScheduleResult:
        return await read_service.schedule_content_campaign(_subject(), campaign_id, publish_at)

    @server.tool(
        name="cancel_content_campaign",
        title="Cancel an unpublished content campaign",
        description="Cancel a draft or scheduled campaign after explicit confirmation. Scheduling credits are not refunded.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    )
    async def cancel_content_campaign(
        campaign_id: str = Field(min_length=36, max_length=36),
    ) -> CampaignCancelResult:
        return await read_service.cancel_content_campaign(_subject(), campaign_id)

    @server.tool(
        name="list_review_reply_jobs",
        title="List AI review reply jobs",
        description=(
            "List generated review reply drafts, approvals, scheduled replies and delivery results. "
            "This read-only action costs no credits."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    async def list_review_reply_jobs(
        status: Literal["all", "draft", "pending_approval", "scheduled", "publishing", "published", "failed", "cancelled"] = "all",
        limit: int = Field(default=50, ge=1, le=200),
    ) -> ReviewReplyJobList:
        return await read_service.list_review_reply_jobs(_subject(), status, limit)

    @server.tool(
        name="approve_review_reply_job",
        title="Approve an AI review reply",
        description=(
            "Approve the exact stored reply text for publication now or at a supplied time. "
            "Publishing costs 2.5 credits. Call only after showing the user the draft, target "
            "review, publication time and charge."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def approve_review_reply_job(
        job_id: str = Field(min_length=36, max_length=36),
        publish_at: datetime | None = None,
    ) -> ReviewReplyApprovalResult:
        return await read_service.approve_review_reply_job(_subject(), job_id, publish_at)

    @server.tool(
        name="cancel_review_reply_job",
        title="Cancel an AI review reply",
        description="Cancel a draft, pending, scheduled or failed review reply. No credits are refunded.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    )
    async def cancel_review_reply_job(
        job_id: str = Field(min_length=36, max_length=36),
    ) -> ReviewReplyCancellationResult:
        return await read_service.cancel_review_reply_job(_subject(), job_id)

    @server.tool(
        name="list_media_assets",
        title="List uploaded media assets",
        description=(
            "List reusable photos and videos already uploaded to the authenticated account's "
            "media library. Use an asset ID when creating a content campaign."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    async def list_media_assets(
        media_kind: Literal["all", "photo", "video"] = "all",
        limit: int = Field(default=50, ge=1, le=200),
    ) -> MediaAssetList:
        return await read_service.list_media_assets(_subject(), media_kind, limit)

    @server.tool(
        name="list_reviews",
        title="List customer reviews",
        description=(
            "List reviews for one owned Google Business Profile location. Filter by "
            "whether a review has a reply. This read-only action costs no credits."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_reviews(
        location_id: str,
        reply_status: Literal["all", "unanswered", "answered"] = "all",
        limit: int = Field(default=20, ge=1, le=50),
        page_token: str | None = Field(default=None, max_length=500),
    ) -> ReviewList:
        return await read_service.list_reviews(
            _subject(), location_id, reply_status, limit, page_token
        )

    @server.tool(
        name="draft_review_reply",
        title="Draft a review reply",
        description=(
            "Draft a reply for one unanswered review using the location's saved tone, "
            "keywords, and instructions. This does not publish or spend credits."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def draft_review_reply(
        location_id: str,
        review_id: str = Field(max_length=500),
    ) -> ReviewDraft:
        return await read_service.draft_review_reply(_subject(), location_id, review_id)

    @server.tool(
        name="publish_review_reply",
        title="Publish a review reply",
        description=(
            "Publish the exact supplied reply publicly on Google. Call only after the "
            "user has reviewed and explicitly confirmed the reply and the 2.5-credit "
            "charge."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def publish_review_reply(
        location_id: str,
        review_id: str = Field(max_length=500),
        reply_text: str = Field(min_length=1, max_length=4096),
    ) -> PublishedReply:
        return await read_service.publish_review_reply(
            _subject(), location_id, review_id, reply_text
        )

    @server.tool(
        name="list_scheduled_posts",
        title="List scheduled posts",
        description=(
            "List calendar posts for one owned location. This read-only action costs "
            "no credits."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def list_scheduled_posts(
        location_id: str,
        status: Literal["all", "scheduled", "publishing", "published", "failed"] = "scheduled",
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        limit: int = Field(default=50, ge=1, le=100),
    ) -> ScheduledPostList:
        return await read_service.list_scheduled_posts(
            _subject(), location_id, status, from_time, to_time, limit
        )

    @server.tool(
        name="schedule_post",
        title="Schedule a business post",
        description=(
            "Schedule an approved text, photo, or video item for Google publication. "
            "Call only after the user explicitly confirms the location, content, exact "
            "time, and 5-credit charge. Media must use a durable public HTTPS URL."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def schedule_post(
        location_id: str,
        publish_at: datetime,
        caption: str = Field(default="", max_length=1500),
        post_type: Literal["LOCAL_POST", "PHOTO", "VIDEO"] = "LOCAL_POST",
        media_url: HttpUrl | None = None,
    ) -> ScheduledPostResult:
        return await read_service.schedule_post(
            _subject(), location_id, publish_at, caption, post_type, media_url
        )

    @server.tool(
        name="cancel_scheduled_post",
        title="Cancel a scheduled post",
        description=(
            "Permanently remove one pending scheduled post. Call only after the user "
            "explicitly confirms cancellation. The original 5-credit charge is not refunded."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def cancel_scheduled_post(
        location_id: str,
        post_id: str = Field(max_length=100),
    ) -> CancelledPost:
        return await read_service.cancel_scheduled_post(
            _subject(), location_id, post_id
        )

    return server


def mcp_transport_security() -> TransportSecuritySettings:
    resource_url = os.getenv("MCP_RESOURCE_URL", DEFAULT_MCP_RESOURCE_URL)
    resource_host = urlparse(resource_url).netloc
    allowed_origins = [
        origin.strip()
        for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            resource_host,
            f"{resource_host}:*",
            "localhost:*",
            "127.0.0.1:*",
            "testserver",
        ],
        allowed_origins=allowed_origins
        + ["http://localhost:*", "http://127.0.0.1:*"],
    )
