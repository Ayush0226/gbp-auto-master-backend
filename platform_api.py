"""Account-first API surface for GBP Auto Master platform workflows."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, HttpUrl, model_validator
from starlette.concurrency import run_in_threadpool

from security import normalize_location


router = APIRouter(prefix="/api/platform", tags=["platform-v2"])


def _user_id(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Sign in to continue")
    return user.id


async def _execute(call):
    return await run_in_threadpool(call)


async def _owned_locations(db, user_id: str, location_ids: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(normalize_location(value) for value in location_ids))
    if not normalized:
        raise HTTPException(422, "Select at least one location")
    result = await _execute(
        lambda: db.table("location_profiles")
        .select("location_id")
        .eq("user_id", user_id)
        .in_("location_id", normalized)
        .execute()
    )
    owned = {row["location_id"] for row in result.data or []}
    if owned != set(normalized):
        raise HTTPException(403, "One or more locations do not belong to this account")
    return normalized


class PreferencesUpdate(BaseModel):
    timezone: str = Field(default="Asia/Kolkata", min_length=1, max_length=100)
    locale: str = Field(default="en-IN", min_length=2, max_length=20)
    default_account_id: str | None = Field(default=None, max_length=200)
    default_location_id: str | None = Field(default=None, max_length=200)


class AutomationRuleInput(BaseModel):
    location_id: str | None = None
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = False
    mode: Literal["draft", "approval", "auto_publish"] = "draft"
    min_rating: int = Field(default=4, ge=1, le=5)
    max_rating: int = Field(default=5, ge=1, le=5)
    tone: str = Field(default="friendly professional", min_length=1, max_length=100)
    language: str = Field(default="auto", min_length=2, max_length=20)
    custom_instructions: str = Field(default="", max_length=2000)
    delay_minutes: int = Field(default=0, ge=0, le=10080)
    daily_limit: int = Field(default=20, ge=1, le=500)
    blocked_terms: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_rating_range(self):
        if self.min_rating > self.max_rating:
            raise ValueError("Minimum rating cannot exceed maximum rating")
        self.blocked_terms = list(
            dict.fromkeys(term.strip() for term in self.blocked_terms if term.strip())
        )
        return self


class CampaignInput(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    topic_type: Literal["STANDARD", "EVENT", "OFFER"] = "STANDARD"
    summary: str = Field(default="", max_length=1500)
    timezone: str = Field(default="Asia/Kolkata", min_length=1, max_length=100)
    location_ids: list[str] = Field(min_length=1, max_length=100)
    call_to_action: dict[str, Any] | None = None
    event_details: dict[str, Any] | None = None
    offer_details: dict[str, Any] | None = None
    media_asset_id: UUID | None = None

    @model_validator(mode="after")
    def post_type_fields(self):
        if self.topic_type in {"EVENT", "OFFER"} and not self.event_details:
            raise ValueError("Event start and end details are required")
        if self.topic_type == "OFFER" and not self.offer_details:
            raise ValueError("Offer details are required")
        return self


class CampaignScheduleInput(BaseModel):
    publish_at: datetime

    @model_validator(mode="after")
    def future_timestamp(self):
        value = self.publish_at
        if value.tzinfo is None:
            raise ValueError("Publication time must include a timezone")
        if value.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("Publication time must be in the future")
        return self


class MediaAssetInput(BaseModel):
    object_path: str = Field(min_length=1, max_length=500)
    public_url: HttpUrl | None = None
    media_kind: Literal["photo", "video"]
    mime_type: str = Field(min_length=3, max_length=100)
    byte_size: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    duration_seconds: float | None = Field(default=None, ge=0)


class ReviewJobApprovalInput(BaseModel):
    publish_at: datetime | None = None

    @model_validator(mode="after")
    def valid_timestamp(self):
        if self.publish_at is not None:
            if self.publish_at.tzinfo is None:
                raise ValueError("Publication time must include a timezone")
            if self.publish_at.astimezone(timezone.utc) < datetime.now(timezone.utc):
                raise ValueError("Publication time cannot be in the past")
        return self


@router.get("/overview")
async def account_overview(request: Request):
    db, user_id = request.app.state.db, _user_id(request)
    account, subscriptions, accounts, locations, rules, campaigns = await _execute(
        lambda: (
            db.rpc("ensure_account", {"p_user": user_id}).execute().data,
            db.table("account_subscriptions").select("*").eq("user_id", user_id).limit(1).execute().data or [],
            db.table("gbp_accounts").select("resource_name,display_name,account_type,role,verification_state,last_synced_at").eq("user_id", user_id).execute().data or [],
            db.table("location_profiles").select("location_id,account_id").eq("user_id", user_id).execute().data or [],
            db.table("review_automation_rules").select("id,enabled,mode,location_id").eq("user_id", user_id).execute().data or [],
            db.table("content_campaigns").select("id,status").eq("user_id", user_id).execute().data or [],
        )
    )
    subscription = subscriptions[0] if subscriptions else None
    return {
        "subscription": subscription or {"plan_type": "free", "status": "active"},
        "credits": float((account or {}).get("tokens_balance") or 0),
        "google_accounts": accounts,
        "location_count": len(locations),
        "automation": {
            "rule_count": len(rules),
            "enabled_rule_count": sum(1 for rule in rules if rule.get("enabled")),
            "auto_publish_rule_count": sum(1 for rule in rules if rule.get("enabled") and rule.get("mode") == "auto_publish"),
        },
        "campaigns": {
            "total": len(campaigns),
            "scheduled": sum(1 for item in campaigns if item.get("status") == "scheduled"),
            "failed": sum(1 for item in campaigns if item.get("status") in {"failed", "partially_published"}),
        },
    }


@router.get("/accounts")
async def list_accounts(request: Request):
    db, user_id = request.app.state.db, _user_id(request)
    accounts, locations = await _execute(
        lambda: (
            db.table("gbp_accounts").select("*").eq("user_id", user_id).order("display_name").execute().data or [],
            db.table("location_profiles").select("location_id,account_id").eq("user_id", user_id).execute().data or [],
        )
    )
    counts: dict[str, int] = {}
    for location in locations:
        counts[location["account_id"]] = counts.get(location["account_id"], 0) + 1
    return {"accounts": [{**account, "location_count": counts.get(account["resource_name"], 0)} for account in accounts]}


@router.put("/preferences")
async def update_preferences(payload: PreferencesUpdate, request: Request):
    db, user_id = request.app.state.db, _user_id(request)
    if payload.default_location_id:
        payload.default_location_id = (await _owned_locations(db, user_id, [payload.default_location_id]))[0]
    if payload.default_account_id:
        result = await _execute(lambda: db.table("gbp_accounts").select("resource_name").eq("resource_name", payload.default_account_id).eq("user_id", user_id).execute())
        if not result.data:
            raise HTTPException(403, "Google Business Profile account is not owned by this user")
    row = {"user_id": user_id, **payload.model_dump(), "updated_at": datetime.now(timezone.utc).isoformat()}
    saved = await _execute(lambda: db.table("account_preferences").upsert(row, on_conflict="user_id").execute())
    return {"status": "success", "preferences": (saved.data or [row])[0]}


@router.get("/automation-rules")
async def list_automation_rules(request: Request):
    db, user_id = request.app.state.db, _user_id(request)
    result = await _execute(lambda: db.table("review_automation_rules").select("*").eq("user_id", user_id).order("created_at").execute())
    return {"rules": result.data or []}


@router.put("/automation-rules")
async def upsert_automation_rule(payload: AutomationRuleInput, request: Request):
    db, user_id = request.app.state.db, _user_id(request)
    location_id = None
    if payload.location_id:
        location_id = (await _owned_locations(db, user_id, [payload.location_id]))[0]
    row = {
        **payload.model_dump(exclude={"location_id"}),
        "user_id": user_id,
        "location_id": location_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    query = db.table("review_automation_rules").select("id").eq("user_id", user_id)
    query = query.eq("location_id", location_id) if location_id else query.is_("location_id", "null")
    existing = await _execute(query.execute)
    if existing.data:
        saved = await _execute(lambda: db.table("review_automation_rules").update(row).eq("id", existing.data[0]["id"]).eq("user_id", user_id).execute())
    else:
        saved = await _execute(lambda: db.table("review_automation_rules").insert(row).execute())
    return {"status": "success", "rule": saved.data[0]}


@router.get("/campaigns")
async def list_campaigns(
    request: Request,
    status: str | None = Query(default=None, max_length=30),
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    db, user_id = request.app.state.db, _user_id(request)
    query = db.table("content_campaigns").select("*").eq("user_id", user_id)
    if status:
        query = query.eq("status", status)
    if from_time:
        query = query.gte("scheduled_for", from_time.isoformat())
    if to_time:
        query = query.lte("scheduled_for", to_time.isoformat())
    campaigns = (await _execute(lambda: query.order("created_at", desc=True).limit(limit).execute())).data or []
    ids = [item["id"] for item in campaigns]
    deliveries = []
    if ids:
        deliveries = (await _execute(lambda: db.table("campaign_locations").select("*").eq("user_id", user_id).in_("campaign_id", ids).execute())).data or []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for delivery in deliveries:
        grouped.setdefault(delivery["campaign_id"], []).append(delivery)
    return {"campaigns": [{**campaign, "locations": grouped.get(campaign["id"], [])} for campaign in campaigns]}


@router.post("/campaigns")
async def create_campaign(payload: CampaignInput, request: Request):
    db, user_id = request.app.state.db, _user_id(request)
    location_ids = await _owned_locations(db, user_id, payload.location_ids)
    params = {
        "p_user": user_id,
        "p_title": payload.title,
        "p_topic_type": payload.topic_type,
        "p_summary": payload.summary,
        "p_timezone": payload.timezone,
        "p_location_ids": location_ids,
        "p_call_to_action": payload.call_to_action,
        "p_event_details": payload.event_details,
        "p_offer_details": payload.offer_details,
        "p_media_asset": str(payload.media_asset_id) if payload.media_asset_id else None,
    }
    result = await _execute(lambda: db.rpc("create_content_campaign", params).execute())
    return result.data


@router.post("/campaigns/{campaign_id}/schedule")
async def schedule_campaign(campaign_id: UUID, payload: CampaignScheduleInput, request: Request):
    db, user_id = request.app.state.db, _user_id(request)
    result = await _execute(lambda: db.rpc("schedule_content_campaign", {"p_user": user_id, "p_campaign": str(campaign_id), "p_publish_at": payload.publish_at.astimezone(timezone.utc).isoformat()}).execute())
    return result.data


@router.post("/campaigns/{campaign_id}/cancel")
async def cancel_campaign(campaign_id: UUID, request: Request):
    db, user_id = request.app.state.db, _user_id(request)
    result = await _execute(lambda: db.rpc("cancel_content_campaign", {"p_user": user_id, "p_campaign": str(campaign_id)}).execute())
    return result.data


@router.get("/review-jobs")
async def list_review_jobs(
    request: Request,
    status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
):
    db, user_id = request.app.state.db, _user_id(request)
    query = db.table("review_reply_jobs").select("*").eq("user_id", user_id)
    if status:
        query = query.eq("status", status)
    result = await _execute(lambda: query.order("created_at", desc=True).limit(limit).execute())
    return {"jobs": result.data or []}


@router.post("/review-jobs/{job_id}/approve")
async def approve_review_job(job_id: UUID, payload: ReviewJobApprovalInput, request: Request):
    db, user_id = request.app.state.db, _user_id(request)
    existing = await _execute(
        lambda: db.table("review_reply_jobs").select("id,status,draft_text,location_id")
        .eq("id", str(job_id)).eq("user_id", user_id).limit(1).execute()
    )
    if not existing.data:
        raise HTTPException(404, "Review reply job not found")
    job = existing.data[0]
    if job["status"] not in {"draft", "pending_approval", "failed"}:
        raise HTTPException(409, f"A {job['status']} reply cannot be approved")
    publish_at = (payload.publish_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    saved = await _execute(
        lambda: db.table("review_reply_jobs").update({
            "status": "scheduled", "scheduled_for": publish_at, "last_error": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", str(job_id)).eq("user_id", user_id).eq("status", job["status"]).execute()
    )
    if not saved.data:
        raise HTTPException(409, "The reply changed while it was being approved")
    await _execute(lambda: db.table("audit_events").insert({
        "user_id": user_id, "location_id": job["location_id"], "actor_type": "user",
        "action": "approve_reply", "resource_type": "review_reply_job", "resource_id": str(job_id),
        "detail": {"scheduled_for": publish_at, "credits_due": 2.5},
    }).execute())
    return {"status": "scheduled", "job": saved.data[0], "credits_due": 2.5}


@router.post("/review-jobs/{job_id}/cancel")
async def cancel_review_job(job_id: UUID, request: Request):
    db, user_id = request.app.state.db, _user_id(request)
    existing = await _execute(
        lambda: db.table("review_reply_jobs").select("id,status,location_id")
        .eq("id", str(job_id)).eq("user_id", user_id).limit(1).execute()
    )
    if not existing.data:
        raise HTTPException(404, "Review reply job not found")
    job = existing.data[0]
    if job["status"] not in {"draft", "pending_approval", "scheduled", "failed"}:
        raise HTTPException(409, f"A {job['status']} reply cannot be cancelled")
    saved = await _execute(
        lambda: db.table("review_reply_jobs").update({
            "status": "cancelled", "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", str(job_id)).eq("user_id", user_id).eq("status", job["status"]).execute()
    )
    if not saved.data:
        raise HTTPException(409, "The reply changed while it was being cancelled")
    await _execute(lambda: db.table("audit_events").insert({
        "user_id": user_id, "location_id": job["location_id"], "actor_type": "user",
        "action": "cancel_reply", "resource_type": "review_reply_job", "resource_id": str(job_id),
    }).execute())
    return {"status": "cancelled", "job_id": str(job_id), "credits_refunded": 0}


@router.get("/media")
async def list_media(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    db, user_id = request.app.state.db, _user_id(request)
    result = await _execute(lambda: db.table("media_assets").select("*").eq("user_id", user_id).neq("status", "deleted").order("created_at", desc=True).limit(limit).execute())
    return {"assets": result.data or []}


@router.post("/media")
async def register_media(payload: MediaAssetInput, request: Request):
    db, user_id = request.app.state.db, _user_id(request)
    if not payload.object_path.startswith(f"{user_id}/"):
        raise HTTPException(403, "Media must be uploaded inside the signed-in user's folder")
    row = {"user_id": user_id, **payload.model_dump(mode="json"), "status": "uploaded"}
    saved = await _execute(lambda: db.table("media_assets").upsert(row, on_conflict="user_id,storage_bucket,object_path").execute())
    return {"status": "success", "asset": saved.data[0]}


@router.get("/audit")
async def list_audit_events(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    db, user_id = request.app.state.db, _user_id(request)
    result = await _execute(lambda: db.table("audit_events").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute())
    return {"events": result.data or []}
