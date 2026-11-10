import os
import re
from fastapi import FastAPI, HTTPException, Request, Depends, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from supabase import create_client, Client
from supabase.lib.client_options import SyncClientOptions
import httpx
from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local", override=False)

from security import authorize, normalize_location, track_job
from credential_store import google_refresh_grant
from connection_api import router as connection_router
from image_api import router as image_router
from ops_api import router as ops_router
import billing
from entitlements import resolve_subscription
from mcp_auth import protected_resource_metadata
from uuid import uuid4
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool
from mcp_server import create_gbp_mcp_server, mcp_transport_security
from platform_api import router as platform_router
from rank_service import (
    create_rank_pdf_token, read_rank_pdf_token,
    render_rank_report_pdf, run_local_rank_scan,
)


@asynccontextmanager
async def lifespan(application):
    required = ('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY',
                'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GROQ_API_KEY')
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError('Missing environment variables: ' + ', '.join(missing))
    try:
        (await run_in_threadpool(lambda: application.state.db.table('operation_runs').select('operation_id').limit(0).execute()))
    except Exception:
        raise RuntimeError('Database migration 004 must be applied before starting this backend') from None
    try:
        (await run_in_threadpool(lambda: application.state.db.table('calendar_posts').select('publish_at').limit(0).execute()))
    except Exception:
        raise RuntimeError('Database migration 002 must be applied before starting this backend') from None
    try:
        (await run_in_threadpool(lambda: application.state.db.table('review_reply_jobs').select('id').limit(0).execute()))
    except Exception:
        raise RuntimeError('Database migration 003 must be applied before starting this backend') from None
    try:
        await run_in_threadpool(lambda: application.state.db.table('account_subscriptions').select('is_lifetime,access_scope').limit(0).execute())
    except Exception:
        raise RuntimeError('Database migration 005 must be applied before starting this backend') from None
    try:
        await run_in_threadpool(lambda: application.state.db.table('job_runs').select('lease_owner,lease_expires_at').limit(0).execute())
        await run_in_threadpool(lambda: application.state.db.table('provider_credentials').select('provider').limit(0).execute())
        from credential_store import cipher
        cipher()
    except Exception:
        raise RuntimeError('Apply the reliability migration and configure CREDENTIAL_ENCRYPTION_KEY before deployment') from None
    async with gbp_mcp.session_manager.run():
        yield

app = FastAPI(title="GBP Auto Master Backend", lifespan=lifespan, dependencies=[Depends(authorize), Depends(track_job)])

# Allow frontend to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=[x.strip() for x in os.getenv("ALLOWED_ORIGINS", "https://www.gbpautomaster.in,https://gbpautomaster.in,http://localhost:5173").split(",") if x.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)

# Initialize Supabase Client
supabase_url = os.getenv('SUPABASE_URL', '')
supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')

# Only initialize if keys are present
if supabase_url and supabase_key:
    # Avoid reusing idle HTTP/2 connections after a free-tier wake-up. Disable
    # generic write retries: a lost response must not replay a non-idempotent RPC.
    supabase: Client = create_client(supabase_url, supabase_key, options=SyncClientOptions(
        httpx_client=httpx.Client(http2=False, timeout=httpx.Timeout(30,connect=5),
            limits=httpx.Limits(max_connections=20,max_keepalive_connections=0)),
        postgrest_client_retry=False, auto_refresh_token=False, persist_session=False,
    ))
else:
    supabase = None

app.state.db = supabase
app.include_router(platform_router)
app.include_router(connection_router)
app.include_router(image_router)
app.include_router(ops_router)


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
async def oauth_protected_resource_metadata():
    """Advertise the Supabase authorization server to MCP clients."""
    return protected_resource_metadata()


@app.get("/.well-known/openai-apps-challenge", response_class=PlainTextResponse)
async def openai_apps_challenge():
    token = os.getenv("OPENAI_APPS_CHALLENGE", "").strip()
    if not token:
        raise HTTPException(404, "Domain verification challenge is not configured")
    return token


@app.get("/api/rank/report.pdf")
async def download_rank_report_pdf(token: str = Query(min_length=40, max_length=12000)):
    secret = os.getenv("RANK_REPORT_SIGNING_SECRET") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    try:
        report = read_rank_pdf_token(token, secret)
    except TimeoutError as error:
        raise HTTPException(410, str(error)) from error
    except ValueError as error:
        raise HTTPException(403, str(error)) from error
    filename = re.sub(r"[^A-Za-z0-9_-]+", "-", str(report.get("keyword") or "keyword")).strip("-")[:60]
    return Response(
        content=render_rank_report_pdf(report),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="GBP-Rank-{filename or "report"}.pdf"'},
    )

class RequestModel(BaseModel):
    @field_validator('location_id', check_fields=False)
    @classmethod
    def canonical_location(cls, value):
        return normalize_location(value) if value is not None else None

PRICING_PLANS = {
    'free':        {'price': 0,    'max_keywords': 2,  'competitor': False, 'duration_months': 0},
    'monthly':     {'price': 500,  'max_keywords': 5,  'competitor': False, 'duration_months': 1},
    'half_yearly': {'price': 2500, 'max_keywords': 10, 'competitor': True,  'duration_months': 6},
    'yearly':      {'price': 5000, 'max_keywords': 15, 'competitor': True,  'duration_months': 12},
}

class OnboardingRequest(RequestModel):
    user_id: str | None = None
    reply_length: str  # '10-40', '50-90', '100-120'
    seo_keywords: list[str]
    full_name: str = None

class BatchReplyRequest(RequestModel):
    user_id: str
    location_id: str
    account_id: str
    access_token: str
    count: int = Field(default=5, ge=1, le=25)

class RegenerateReplyRequest(RequestModel):
    user_id: str
    review_name: str
    review_text: str
    star_rating: str
    location_id: str
    access_token: str

class RankReportRequest(RequestModel):
    user_id: str
    keyword: str = Field(min_length=1, max_length=120)
    location_id: str
    request_id: str = Field(min_length=8, max_length=100)

class UserProfileRequest(RequestModel):
    user_id: str
    location_id: str | None = None

from datetime import date, datetime, timedelta, timezone

async def ensure_location_profile(location_id: str, user_id: str) -> dict:
    rows = (await run_in_threadpool(lambda: supabase.table('location_profiles').select('*').eq('location_id', normalize_location(location_id)).eq('user_id', user_id).execute())).data
    if not rows:
        raise HTTPException(409, 'Reconnect Google locations before continuing')
    profile = rows[0]
    subscriptions = (await run_in_threadpool(lambda: supabase.table('account_subscriptions').select('*').eq('user_id', user_id).execute())).data or []
    if subscriptions:
        subscription = resolve_subscription(subscriptions[0])
        profile.update({key: subscription[key] for key in ('plan_type', 'is_lifetime', 'unlimited_access', 'access_scope', 'max_seo_keywords', 'competitor_access')})
        profile['subscription_end'] = subscription['expires_at']
        profile['auto_renew'] = bool(subscription.get('auto_renew'))
    else:
        end = profile.get('subscription_end')
        if profile.get('plan_type') != 'free' and (not end or datetime.fromisoformat(end.replace('Z', '+00:00')).timestamp() <= datetime.now().timestamp()):
            profile['plan_type'] = 'free'
    return {key: value for key, value in profile.items() if key not in {'monthly_token_allowance', 'next_token_grant_at'}}

async def ensure_user_profile(user_id: str) -> dict:
    profile = (await run_in_threadpool(lambda: supabase.rpc('ensure_account', {'p_user': user_id}).execute())).data
    return {key: value for key, value in profile.items() if key not in {'tokens_balance', 'last_daily_claim', 'monthly_token_allowance', 'next_token_grant_at'}}

@app.post("/api/payment/create-order")
@app.post("/api/payment/verify")
async def payment_checkout_paused():
    billing.checkout_paused()


class AccessCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=100)


@app.post('/api/billing/redeem-access-code')
async def redeem_access_code(req: AccessCodeRequest, request: Request):
    user_id = request.state.user.id
    return await run_in_threadpool(billing.redeem_access_code, request.app.state.db, user_id, req.code)

class CancelSubscriptionRequest(RequestModel):
    user_id: str
    location_id: str | None = None

@app.post("/api/billing/cancel")
async def cancel_subscription(req: CancelSubscriptionRequest):
    profile = await ensure_user_profile(req.user_id)
    if profile.get('is_lifetime'):
        return {'status': 'success', 'message': 'Lifetime access has no renewal to cancel.', 'subscription_end': None, 'is_lifetime': True}
    rows = (await run_in_threadpool(lambda: supabase.table('account_subscriptions').select('expires_at').eq('user_id', req.user_id).execute())).data or []
    (await run_in_threadpool(lambda: supabase.table('account_subscriptions').update({'auto_renew': False, 'updated_at': datetime.now(timezone.utc).isoformat()}).eq('user_id', req.user_id).execute()))
    (await run_in_threadpool(lambda: supabase.table('location_profiles').update({'auto_renew': False}).eq('user_id', req.user_id).execute()))
    return {'status': 'success', 'message': 'This is a prepaid account plan with no automatic renewal. Access continues until expiry.', 'subscription_end': rows[0].get('expires_at') if rows else None}

@app.get("/api/payment/key")
async def payment_key_unavailable():
    billing.checkout_paused()

@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "service": "gbp-auto-master-backend"}


@app.get('/api/ready')
async def readiness_check():
    if app.state.db is None:
        raise HTTPException(503, 'Database is not configured')
    try:
        await run_in_threadpool(lambda: app.state.db.table('operation_runs').select('operation_id').limit(0).execute())
        await run_in_threadpool(lambda: app.state.db.table('review_reply_jobs').select('id').limit(0).execute())
        await run_in_threadpool(lambda: app.state.db.table('account_subscriptions').select('is_lifetime,access_scope').limit(0).execute())
        await run_in_threadpool(lambda: app.state.db.table('job_runs').select('lease_owner,lease_expires_at').limit(0).execute())
        await run_in_threadpool(lambda: app.state.db.table('provider_credentials').select('provider').limit(0).execute())
    except Exception:
        raise HTTPException(503, 'Database or required schema is unavailable') from None
    return {'status': 'ready', 'service': 'gbp-auto-master-backend', 'release':'2026-11-10-reliability'}

class SaveAISettingsRequest(RequestModel):
    user_id: str
    location_id: str
    settings: dict

class GetAISettingsRequest(RequestModel):
    user_id: str
    location_id: str

@app.post("/api/user/get-ai-settings")
async def get_ai_settings(req: GetAISettingsRequest):
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    try:
        user_data = (await run_in_threadpool(lambda: supabase.auth.admin.get_user_by_id(req.user_id)))
        if not user_data.user:
            raise HTTPException(status_code=404, detail="User not found")
            
        user_meta = user_data.user.user_metadata or {}
        ai_settings = user_meta.get("ai_settings", {})
        
        settings = dict(ai_settings.get(req.location_id, {}))
        profile = await ensure_location_profile(req.location_id, req.user_id)
        limit = None if profile.get('unlimited_access') else PRICING_PLANS[profile['plan_type']]['max_keywords']
        keywords = settings.get('active_keywords', [])
        settings['active_keywords'] = [k for k in keywords if isinstance(k,str)][:limit] if isinstance(keywords,list) else []
        return settings
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

@app.post("/api/user/save-ai-settings")
async def save_ai_settings(req: SaveAISettingsRequest):
    profile = await ensure_location_profile(req.location_id, req.user_id)
    limit = None if profile.get('unlimited_access') else PRICING_PLANS[profile['plan_type']]['max_keywords']
    keywords = req.settings.get('active_keywords', [])
    if not isinstance(keywords, list) or any(not isinstance(k,str) or len(k)>120 for k in keywords):
        raise HTTPException(422, 'Keywords must be a list of short text values')
    if limit is not None and len(keywords)>limit:
        raise HTTPException(422, f'This plan allows {limit} keywords')
    user = (await run_in_threadpool(lambda: supabase.auth.admin.get_user_by_id(req.user_id))).user
    settings = (user.user_metadata or {}).get('ai_settings', {})
    allowed = {'is_ai_active','reply_to_1_star','ai_tone','custom_instructions','active_keywords','search_keywords'}
    settings[req.location_id] = {k:v for k,v in req.settings.items() if k in allowed}
    (await run_in_threadpool(lambda: supabase.auth.admin.update_user_by_id(req.user_id, {'user_metadata': {'ai_settings': settings}})))
    return {'status':'success','message':'AI settings saved'}

class AdminAuthRequest(RequestModel):
    admin_email: str

@app.post("/api/admin/users")
async def get_all_users(req: AdminAuthRequest):
        
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
        
    try:
        users = (await run_in_threadpool(lambda: list_all_users()))
        user_list = []
        for u in users:
            meta = u.user_metadata or {}
            user_list.append({
                "id": u.id,
                "email": u.email,
                "created_at": str(u.created_at),
                "full_name": meta.get("full_name"),
                "demo_used": meta.get("demo_used", False),
                "subscriptions": await location_subscriptions(u.id),
                "has_google_token": bool(meta.get("google_refresh_token")),
                "user_metadata": {k: meta[k] for k in ("ai_settings", "competitor_intel", "cached_locations") if k in meta}
            })
        return {"status": "success", "users": user_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

class AdminUserRequest(RequestModel):
    admin_email: str
    target_user_id: str

@app.post("/api/admin/calendar")
async def admin_get_calendar(req: AdminUserRequest):
        
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
        
    try:
        posts = (await run_in_threadpool(lambda: supabase.table('calendar_posts').select('*').eq('user_id', req.target_user_id).order('post_date', desc=True).execute()))
        return {"status": "success", "posts": posts.data or []}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

@app.post("/api/admin/run-competitor-scan")
async def run_competitor_scan(req: AdminAuthRequest):
        
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
        
    import datetime as dt
    from groq import Groq
    try:
        users = (await run_in_threadpool(lambda: list_all_users()))
        scanned_count = 0
        
        for u in users:
            meta = u.user_metadata or {}
            subs = await location_subscriptions(u.id)
            intel = meta.get('competitor_intel', {})
            
            # For this MVP demo, let's generate intel for loc1 or any active subscription
            loc_ids = list(subs.keys())
            
            # Only generate if they have actually used the demo or connected (Bypass for admin testing)
            if not meta.get('demo_used') and not subs and u.email != 'ayushsony126@gmail.com':
                continue
            
            for loc_id in loc_ids:
                real_business_name = None
                business_city = None
                business_country = None
                user_actual_rating = "N/A"
                user_actual_reviews = "N/A"
                refresh_token = await run_in_threadpool(google_refresh_grant, supabase, u)
                from http_client import requests
                if refresh_token:
                    try:
                        access_token = (await run_in_threadpool(lambda: get_offline_access_token(refresh_token)))
                        clean_loc_id = loc_id if loc_id.startswith('locations/') else f"locations/{loc_id}"
                        headers = {"Authorization": f"Bearer {access_token}"}
                        loc_url = f"https://mybusinessbusinessinformation.googleapis.com/v1/{clean_loc_id}?readMask=name,title,storefrontAddress"
                        loc_resp = (await run_in_threadpool(lambda: requests.get(loc_url, headers=headers)))
                        if loc_resp.ok:
                            loc_data = loc_resp.json()
                            real_business_name = loc_data.get('title')
                            address = loc_data.get('storefrontAddress', {})
                            business_city = address.get('locality')
                            business_country = address.get('regionCode')
                            
                        acc_url = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
                        acc_resp = (await run_in_threadpool(lambda: requests.get(acc_url, headers=headers)))
                        if acc_resp.ok:
                            accounts = acc_resp.json().get('accounts', [])
                            if accounts:
                                account_name = (await run_in_threadpool(lambda: registered_account(loc_id)))
                                rev_url = f"https://mybusiness.googleapis.com/v4/{account_name}/{clean_loc_id}/reviews"
                                rev_resp = (await run_in_threadpool(lambda: requests.get(rev_url, headers=headers)))
                                if rev_resp.ok:
                                    rev_data = rev_resp.json()
                                    user_actual_rating = rev_data.get('averageRating', 0.0)
                                    user_actual_reviews = rev_data.get('totalReviewCount', len(rev_data.get('reviews', [])))
                    except Exception as e:
                        print("Failed to fetch real business name/address/reviews:", e)

                # 1. Fetch user's SEO keywords to know what to search for
                base_query = real_business_name or "Local Business"
                try:
                    all_settings = meta.get("ai_settings", {})
                    loc_settings = all_settings.get(loc_id, {})
                    if loc_settings.get('active_keywords'):
                        base_query = loc_settings.get('active_keywords')[0]
                except Exception as e:
                    print("Error getting keywords from metadata:", e)
                    
                # 2. Call SerpApi to get real Google Maps data
                serpapi_key = os.getenv("SERPAPI_KEY")
                search_query = f"{base_query} in {business_city}" if business_city else base_query
                
                params = {
                    "engine": "google_local",
                    "q": search_query,
                    "api_key": serpapi_key
                }
                
                # Use country code if available, but avoid strict 'location' parameter to prevent SerpApi errors
                if business_country:
                    params["gl"] = business_country.lower()
                
                leaderboard = []
                user_rank = 10
                
                try:
                    res = (await run_in_threadpool(lambda: requests.get("https://serpapi.com/search", params=params)))
                    data = res.json()
                    
                    if not res.ok or data.get('error') or not data.get('local_results'):
                        continue
                    local_results = data['local_results']

                    for idx, place in enumerate(local_results[:10]):
                        name = place.get('title') or 'Unknown'
                        is_user = False
                        # Simple fuzzy match to see if this is the user's business
                        target_name = (real_business_name or meta.get('full_name') or '').lower()
                        # Better fuzzy match (remove punctuation)
                        import re
                        clean_target = re.sub(r'[^\w\s]', '', target_name).strip()
                        clean_name = re.sub(r'[^\w\s]', '', name.lower()).strip()
                        
                        if clean_target and len(clean_target) > 3 and (clean_target in clean_name or clean_name in clean_target):
                            is_user = True
                            
                        if is_user:
                            user_rank = idx + 1
                            
                        leaderboard.append({
                            "rank": idx + 1,
                            "name": name + (" (You)" if is_user else ""),
                            "rating": place.get('rating'),
                            "reviews": int(place.get('reviews', 0)),
                            "is_user": is_user
                        })
                        
                    # If the user still wasn't found in the top 10, append them at the end as unranked
                    if user_rank == 10 and not any(l['is_user'] for l in leaderboard):
                        user_rank = 11
                        leaderboard.append({
                            "rank": "11+",
                            "name": (real_business_name or meta.get('full_name') or 'Your Business') + " (You)",
                            "rating": user_actual_rating,
                            "reviews": user_actual_reviews,
                            "is_user": True
                        })
                except Exception as e:
                    print("SerpApi Error:", e)
                    continue

                prompt = f"""You are an expert Local SEO consultant.
Here is the LIVE Google Maps leaderboard for the search '{search_query}':
{leaderboard}

The client is currently at Rank {user_rank}.
Write a professional, concise report in EXACTLY this format:
PROS:
- (1 bullet point on what they are doing right based on their rank/reviews)
CONS:
- (1 bullet point on why competitors are beating them)
ACTION PLAN:
- (2 bullet points on exactly how to outrank them)
Do not include any other text."""
                
                groq_api_key = os.getenv("GROQ_API_KEY", "")
                ai_report = "🎯 Ensure your AI is turned ON this week to respond instantly and boost local engagement.\n🏆 Ask your next 10 customers for reviews to catch up to the next spot.\n💡 Keep injecting your SEO keywords into review replies."
                
                if groq_api_key:
                    try:
                        chat_completion = (await run_in_threadpool(lambda: call_groq_with_fallback(groq_api_key, [{"role": "user", "content": prompt}])))
                        ai_report = chat_completion.choices[0].message.content
                    except Exception as e:
                        print("Groq Error:", e)
                
                intel[loc_id] = {
                    "last_scanned": dt.datetime.now().isoformat(),
                    "leaderboard": leaderboard,
                    "ai_report": ai_report
                }
                scanned_count += 1

                
            # Save back to Supabase
            (await run_in_threadpool(lambda: supabase.auth.admin.update_user_by_id(u.id, {"user_metadata": {"competitor_intel": intel}})))
            
        return {"status": "success", "message": f"Successfully ran competitor scan and generated AI Reports for {scanned_count} locations."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

@app.get("/api/cron/reply-reviews")
async def cron_reply_reviews():
    automated = await publish_due_review_reply_jobs()
    legacy = await run_review_job()
    return {'status': 'success', 'automated': automated, 'legacy': legacy}


# ==========================================
# GOOGLE BUSINESS PROFILE & AI ENGINE
# ==========================================

from http_client import requests
from groq import Groq
import itertools

# We use the user-provided Groq key securely from Environment Variables
# Render Deployment Trigger Update
def call_groq_with_fallback(api_key: str, messages: list, temperature: float = 0.2):
    client = Groq(api_key=api_key or os.getenv('GROQ_API_KEY'), timeout=30.0, max_retries=1)
    
    # Dynamically fetch available models to bypass any decommissioned models
    available_models = client.models.list()
    model_id = None
    
    for m in available_models.data:
        m_id = m.id.lower()
        # Exclude moderation/audio/vision/tool models
        if any(x in m_id for x in ['guard', 'vision', 'tool', 'whisper', 'embed', 'classifier']):
            continue
        model_id = m.id
        if 'llama' in m_id:
            break
            
    if not model_id and available_models.data:
        model_id = available_models.data[0].id
        
    return client.chat.completions.create(
        messages=messages, 
        model=model_id, 
        temperature=temperature
    )

def generate_ai_reply(prompt: str) -> str:
    """
    Generates a reply using Groq's Llama 3 model (100% free and lightning fast).
    """
    chat_completion = call_groq_with_fallback(os.getenv('GROQ_API_KEY'), [
        {
            "role": "system",
            "content": "You are a highly skilled, warm, and professional local business owner. You reply to customer reviews thoughtfully and engagingly. Ensure every reply is unique, polite, and doesn't sound like a generic AI template. Keep it short (max 2-3 sentences)."
        },
        {
            "role": "user",
            "content": prompt,
        }
    ], temperature=0.75)
    return chat_completion.choices[0].message.content

class GoogleSyncRequest(RequestModel):
    user_id: str
    provider_token: str = None

from typing import List

class ChatMessage(RequestModel):
    role: str
    content: str

class ChatContextRequest(RequestModel):
    user_id: str
    message: str
    history: List[ChatMessage]
    context_dump: str

@app.post("/api/ai/chat")
async def chat_with_assistant(req: ChatContextRequest):
    try:
        truncated_context = req.context_dump[:15000] if req.context_dump else ""
        messages = [
            {
                "role": "system",
                "content": f"You are a brilliant business consultant AI built into the 'GBP Auto Master' platform. Your job is to help the business owner analyze their Google Business Profile, summarize data, and give strategic advice. Keep your answers concise, actionable, and friendly.\n\nHere is the LIVE data context for the user's connected Google Business Profile right now:\n{truncated_context}"
            }
        ]
        
        for msg in req.history:
            mapped_role = "assistant" if msg.role == "ai" else msg.role
            messages.append({"role": mapped_role, "content": msg.content})
            
        messages.append({"role": "user", "content": req.message})
        
        chat_completion = (await run_in_threadpool(lambda: call_groq_with_fallback(os.getenv('GROQ_API_KEY'), messages)))
        
        return {"status": "success", "reply": chat_completion.choices[0].message.content}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

class ReportContextRequest(RequestModel):
    user_id: str
    context_dump: str

@app.post("/api/ai/generate-report")
async def generate_report(req: ReportContextRequest):
    try:
        prompt = f"Analyze this Google Business Profile context: {req.context_dump}. Write a 3-sentence executive summary and 3 bullet-point action items for the business owner to improve their ranking and engagement. Format exactly as:\nSUMMARY: [text]\nACTION 1: [text]\nACTION 2: [text]\nACTION 3: [text]"
        chat_completion = (await run_in_threadpool(lambda: call_groq_with_fallback(os.getenv('GROQ_API_KEY'), [{"role": "user", "content": prompt}])))
        response_text = chat_completion.choices[0].message.content
        
        # Parse it out
        summary = "Based on your current Google Business Profile metrics, your response rate is excellent, but your competitor rank indicates room for growth. We recommend focusing heavily on injecting your target SEO keywords into all future review replies to gradually boost local map pack visibility."
        actions = ["Turn on the AI Auto-Replier to instantly catch positive sentiment.", "Add up to 3 more hyper-local keywords in your AI Brain Settings.", "Schedule at least 1 Google Post per week."]
        
        if "SUMMARY:" in response_text:
            try:
                summary_part = response_text.split("SUMMARY:")[1].split("ACTION 1:")[0].strip()
                a1 = response_text.split("ACTION 1:")[1].split("ACTION 2:")[0].strip()
                a2 = response_text.split("ACTION 2:")[1].split("ACTION 3:")[0].strip()
                a3 = response_text.split("ACTION 3:")[1].strip()
                if summary_part: summary = summary_part
                if a1 and a2 and a3: actions = [a1, a2, a3]
            except:
                pass
                
        return {"status": "success", "report": {"summary": summary, "action_items": actions}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

@app.post("/api/google/locations")
async def get_google_locations(req: GoogleSyncRequest):
    await ensure_user_profile(req.user_id)
    headers = {'Authorization': f'Bearer {req.provider_token}'}
    accounts = []
    page = None
    while True:
        response = (await run_in_threadpool(lambda: requests.get('https://mybusinessaccountmanagement.googleapis.com/v1/accounts', headers=headers, params={'pageToken': page} if page else {})))
        if not response.ok:
            raise HTTPException(502, 'Google account lookup failed; reconnect Google')
        data = response.json()
        accounts.extend(data.get('accounts', []))
        page = data.get('nextPageToken')
        if not page: break
    locations = []
    for account in accounts:
        synced = await run_in_threadpool(
            lambda account=account: supabase.rpc('sync_gbp_account', {
                'p_resource': account['name'], 'p_user': req.user_id, 'p_data': {
                'display_name': account.get('accountName') or account.get('organizationInfo', {}).get('registeredDomain') or 'Google Business Profile account',
                'account_type': account.get('type'),
                'role': account.get('role'),
                'verification_state': account.get('verificationState'),
            }}).execute().data
        )
        if not synced:
            raise HTTPException(409, 'This Google Business account is already connected to another user; its ownership and permissions were preserved')
        page = None
        while True:
            params = {'readMask': 'name,title', 'pageSize': 100}
            if page: params['pageToken'] = page
            response = (await run_in_threadpool(lambda: requests.get(f"https://mybusinessbusinessinformation.googleapis.com/v1/{account['name']}/locations", headers=headers, params=params)))
            if not response.ok:
                raise HTTPException(502, 'Google location lookup failed')
            data = response.json()
            for location in data.get('locations', []):
                location_id = normalize_location(location['name'])
                (await run_in_threadpool(lambda: supabase.rpc('register_location', {'p_location': location_id, 'p_user': req.user_id, 'p_account': account['name']}).execute()))
                profile = await ensure_location_profile(location_id, req.user_id)
                locations.append({'id': location_id, 'account_id': account['name'], 'name': location.get('title', 'Business'),
                                  'subscribed': profile['plan_type'] != 'free',
                                  'plan_details': {'plan_id': profile['plan_type'], 'expires_at': profile.get('subscription_end'), 'is_lifetime': profile.get('is_lifetime', False), 'unlimited_access': profile.get('unlimited_access', False)}})
            page = data.get('nextPageToken')
            if not page: break
    (await run_in_threadpool(lambda: supabase.auth.admin.update_user_by_id(req.user_id, {'user_metadata': {'cached_locations': locations}})))
    return {'status': 'success', 'locations': locations}

class GoogleReviewRequest(RequestModel):
    user_id: str
    provider_token: str
    location_id: str
    page_token: str | None = Field(default=None, max_length=4096)

@app.post("/api/google/get-reviews")
async def get_google_reviews(req: GoogleReviewRequest):
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        account_name = (await run_in_threadpool(lambda: registered_account(req.location_id)))
        full_location_path = f"{account_name}/{req.location_id}"
        
        url = f"https://mybusiness.googleapis.com/v4/{full_location_path}/reviews"
        params = {'pageSize': 50, 'orderBy': 'updateTime desc'}
        if req.page_token:
            params['pageToken'] = req.page_token
        resp = (await run_in_threadpool(lambda: requests.get(url, headers=headers, params=params)))
        
        if not resp.ok:
            # If Google API fails (e.g. they don't have access or billing is disabled for reviews API)
            raise HTTPException(409 if resp.status_code in (401,403) else 502, 'Could not load Google reviews; reconnect Google if authorization expired')
            
        json_resp = resp.json()
        data = json_resp.get("reviews", [])
        total_review_count = json_resp.get("totalReviewCount", len(data))
        average_rating = json_resp.get("averageRating", 0.0)
        
        recent_answered = sum(1 for r in data if "reviewReply" in r)
        total_fetched = len(data)

        # Format the reviews for the frontend
        formatted_reviews = []
        for r in data:
            formatted_reviews.append({
                "id": r.get('name'),
                "reviewer": r.get('reviewer', {}).get('displayName', 'Anonymous'),
                "rating": r.get('starRating', 'FIVE'),
                "comment": r.get('comment', ''),
                "createTime": r.get('createTime', ''),
                "has_reply": 'reviewReply' in r,
                "reply_comment": r.get('reviewReply', {}).get('comment', '') if 'reviewReply' in r else ''
            })
            
        return {
            "status": "success", 
            "reviews": formatted_reviews,
            "totalReviewCount": total_review_count,
            "averageRating": average_rating,
            "recentAnswered": recent_answered,
            "totalFetched": total_fetched,
            "nextPageToken": json_resp.get('nextPageToken'),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

@app.post("/api/google/run-demo")
async def run_google_demo(req: GoogleReviewRequest):
    # Explicitly invoked demo uses the same accounting and ownership checks.
    return await sync_and_reply_reviews(req)

@app.post("/api/google/sync-reviews")
async def sync_and_reply_reviews(req: GoogleReviewRequest):
    profile = await ensure_location_profile(req.location_id, req.user_id)
    result = await batch_reply_reviews(BatchReplyRequest(user_id=req.user_id, location_id=req.location_id,
        account_id=profile['account_id'], access_token=req.provider_token, count=4))
    sent = sum(1 for reply in result['replied'] if reply['status']=='published')
    failures = [reply for reply in result['replied'] if reply['status']=='failed']
    return {**result, 'status': 'partial' if failures else 'success', 'message': f'AI sent {sent} replies', 'failures': failures}

@app.post("/api/google/register-webhook")
async def register_google_webhook(req: GoogleReviewRequest):
    """
    Tells Google Business Profile API to start pushing new reviews for this account
    to our specific Pub/Sub topic.
    """
    try:
        topic = os.getenv('GOOGLE_PUBSUB_TOPIC')
        if not topic:
            raise HTTPException(503, 'Google review notifications are not configured')
        user = (await run_in_threadpool(lambda: supabase.auth.admin.get_user_by_id(req.user_id))).user
        refresh = await run_in_threadpool(google_refresh_grant, supabase, user)
        if not refresh:
            raise HTTPException(409, 'Reconnect Google Business Profile before enabling notifications')
        token = await run_in_threadpool(get_offline_access_token, refresh)
        headers = {"Authorization": f"Bearer {token}"}
        account_name = (await run_in_threadpool(lambda: registered_account(req.location_id)))

        # Tell Google to send notifications to our topic
        notif_url = f"https://mybusinessnotifications.googleapis.com/v1/{account_name}/notificationSetting"
        payload = {
            "pubsubTopic": topic,
            "notificationTypes": ["NEW_REVIEW", "UPDATED_REVIEW"]
        }
        
        # We need to specify updateMask for PATCH requests in Google APIs
        resp = (await run_in_threadpool(lambda: requests.patch(notif_url, headers=headers, json=payload, params={"updateMask": "pubsubTopic,notificationTypes"})))
        
        if not resp.ok:
            raise HTTPException(502, f"Google rejected notification registration ({resp.status_code})")
        return {"status": "success", "message": "Webhook successfully registered with Google!"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

@app.post("/api/google/draft-reviews")
async def draft_google_reviews(req: GoogleReviewRequest):
    """
    Fetches unreplied reviews and generates AI drafts for them WITHOUT posting them.
    Used for the Admin Approval Queue.
    """
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        acc_url = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
        acc_resp = (await run_in_threadpool(lambda: requests.get(acc_url, headers=headers)))
        if not acc_resp.ok:
            return {"status": "error", "message": f"Google Account Fetch Error: {acc_resp.text}"}
        accounts = acc_resp.json().get('accounts', [])
        if not accounts:
            return {"status": "error", "message": "No Google Business Accounts found."}
        account_name = (await run_in_threadpool(lambda: registered_account(req.location_id)))
        full_location_path = f"{account_name}/{req.location_id}"
        
        url = f"https://mybusiness.googleapis.com/v4/{full_location_path}/reviews"
        resp = (await run_in_threadpool(lambda: requests.get(url, headers=headers)))
        
        if not resp.ok:
            raise HTTPException(409 if resp.status_code in (401,403) else 502, 'Google data could not be loaded; reconnect or retry')
            
        reviews = resp.json().get('reviews', [])
        
        # Find unreplied reviews
        unreplied = [r for r in reviews if 'reviewReply' not in r]
        
        # Fetch user_settings from Supabase user_metadata
        target_keywords = []
        ai_settings = {}
        if supabase:
            try:
                user_data = (await run_in_threadpool(lambda: supabase.auth.admin.get_user_by_id(req.user_id)))
                if user_data.user:
                    all_settings = (user_data.user.user_metadata or {}).get("ai_settings", {})
                    ai_settings = all_settings.get(req.location_id, {})
                    target_keywords = ai_settings.get('active_keywords', [])
            except Exception as e:
                print("Error fetching settings from Supabase metadata:", e)
                
        keyword_instruction = ""
        if target_keywords:
            keyword_list = ", ".join([f'"{k}"' for k in target_keywords])
            keyword_instruction = f"CRITICAL INSTRUCTION: You MUST organically and naturally weave 1 or 2 of these exact SEO keywords into your reply: {keyword_list}. Ensure the reply sounds genuine, appreciative, and warm, like a real human business owner. Do NOT just say 'thanks for the 5 stars'. Make it a high-quality, thoughtful response."
            
        ai_tone = ai_settings.get('ai_tone', 'Professional') if ai_settings else 'Professional'
        custom_instructions = ai_settings.get('custom_instructions', '') if ai_settings else ''
        custom_instruction_text = f"Additional custom instructions from the business owner: {custom_instructions}" if custom_instructions else ""
        
        drafts = []
        
        for r in unreplied[:10]: # Process max 10 to avoid timeouts
            try:
                rating = r.get('starRating', '')
                reviewer_name = r.get('reviewer', {}).get('displayName', 'Valued Customer')
                if rating in ['ONE', 'TWO'] and ai_settings and not ai_settings.get('reply_to_1_star', False):
                    continue # Skip negative reviews if user disabled it
                    
                customer_comment = r.get('comment', '').strip()
                
                if not customer_comment:
                    prompt = f"Customer '{reviewer_name}' just left a {rating}-star rating with NO text. Write a {ai_tone.lower()} and extremely short, creative 'Thank you' reply (max 2 sentences) appreciating their rating. Use their first name if possible. {keyword_instruction} {custom_instruction_text} Do not include placeholders."
                else:
                    prompt = f"Write a {ai_tone.lower()} and extremely short reply (max 2 sentences) to this customer review. Customer Name: '{reviewer_name}'. Customer Rating: {rating}. Customer Comment: '{customer_comment}'. Use their first name if possible. {keyword_instruction} {custom_instruction_text} Do not include placeholders."
                
                ai_reply = (await run_in_threadpool(lambda: generate_ai_reply(prompt)))
                
                drafts.append({
                    "review_id": r.get('name'),
                    "reviewer": r.get('reviewer', {}).get('displayName', 'Anonymous'),
                    "rating": rating,
                    "comment": customer_comment,
                    "draft_reply": ai_reply
                })
            except Exception as inner_e:
                print(f"AI Error on review {r.get('name')}: {str(inner_e)}")
                
        return {
            "status": "success",
            "drafts": drafts
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

class PostReplyRequest(RequestModel):
    provider_token: str
    review_id: str
    reply_text: str

@app.post("/api/google/post-reply")
async def post_review_reply(req: PostReplyRequest):
    """
    Manually post a specific reply to a Google Review.
    """
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        reply_url = f"https://mybusiness.googleapis.com/v4/{req.review_id}/reply"
        reply_resp = (await run_in_threadpool(lambda: requests.put(reply_url, headers=headers, json={"comment": req.reply_text})))
        
        if reply_resp.ok:
            return {"status": "success", "message": "Reply posted successfully!"}
        else:
            raise HTTPException(502, 'Google rejected this reply')
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

class DeleteReplyRequest(RequestModel):
    provider_token: str
    review_id: str

@app.post("/api/google/delete-reply")
async def delete_review_reply(req: DeleteReplyRequest):
    """
    Manually delete a reply from a Google Review.
    """
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        reply_url = f"https://mybusiness.googleapis.com/v4/{req.review_id}/reply"
        reply_resp = (await run_in_threadpool(lambda: requests.delete(reply_url, headers=headers)))
        
        if reply_resp.ok:
            return {"status": "success", "message": "Reply deleted successfully!"}
        else:
            raise HTTPException(502, 'Google rejected the reply deletion')
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

class PublishPostRequest(RequestModel):
    provider_token: str
    location_id: str
    summary: str
    image_url: str = None
    post_type: str = "LOCAL_POST"
    request_id: str | None = Field(default=None, min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')

async def publish_local_post(req: PublishPostRequest):
    try:
        if req.post_type not in ('LOCAL_POST', 'PHOTO'):
            raise HTTPException(422, 'Video publishing is not enabled')
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        acc_url = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
        acc_resp = (await run_in_threadpool(lambda: requests.get(acc_url, headers=headers)))
        if not acc_resp.ok:
            return {"status": "error", "message": f"Google Account Fetch Error: {acc_resp.text}"}
        accounts = acc_resp.json().get('accounts', [])
        if not accounts:
            return {"status": "error", "message": "No Google Business Accounts found."}
        account_name = (await run_in_threadpool(lambda: registered_account(req.location_id)))
        full_location_path = f"{account_name}/{req.location_id}"
        
        if req.post_type == "LOCAL_POST":
            # Uses mybusiness.googleapis.com/v4/accounts/{accountId}/locations/{locationId}/localPosts
            url = f"https://mybusiness.googleapis.com/v4/{full_location_path}/localPosts"
            
            payload = {
                "languageCode": "en-US",
                "summary": req.summary,
                "topicType": "STANDARD"
            }
            
            if req.image_url:
                payload["media"] = [{
                    "mediaFormat": "PHOTO",
                    "sourceUrl": req.image_url
                }]
                
            resp = (await run_in_threadpool(lambda: requests.post(url, headers=headers, json=payload)))
        else:
            # Uses mybusiness.googleapis.com/v4/accounts/{accountId}/locations/{locationId}/media
            url = f"https://mybusiness.googleapis.com/v4/{full_location_path}/media"
            
            payload = {
                "mediaFormat": req.post_type,
                "locationAssociation": {
                    "category": "ADDITIONAL"
                },
                "sourceUrl": req.image_url
            }
            
            if req.summary:
                payload["description"] = req.summary
                
            resp = (await run_in_threadpool(lambda: requests.post(url, headers=headers, json=payload)))
        
        if not resp.ok:
            if resp.status_code >= 500 or resp.status_code == 408:
                raise HTTPException(502, 'Google delivery is uncertain; reconcile it before retrying')
            return {"status": "error", "message": f"Google refused post: {resp.text}"}
            
        return {"status": "success", "post_data": resp.json()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

# ==========================================
# ANALYTICS & SEO
# ==========================================
import datetime

@app.post("/api/google/analytics")
async def get_google_analytics(req: GoogleReviewRequest):
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        # Performance API uses locations/12345 (NO account prefix)
        clean_loc = req.location_id
        if "locations/" in clean_loc:
            clean_loc = "locations/" + clean_loc.split("locations/")[-1]
        else:
            clean_loc = f"locations/{clean_loc}"
            
        url = f"https://businessprofileperformance.googleapis.com/v1/{clean_loc}:fetchMultiDailyMetricsTimeSeries"
        
        # Get metrics for the last 30 days
        import datetime as dt
        end_date = dt.datetime.now()
        start_date = end_date - dt.timedelta(days=30)
        
        params = {
            "dailyMetrics": ["WEBSITE_CLICKS", "CALL_CLICKS", "BUSINESS_DIRECTION_REQUESTS", "BUSINESS_IMPRESSIONS_DESKTOP_MAPS", "BUSINESS_IMPRESSIONS_MOBILE_MAPS", "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH", "BUSINESS_IMPRESSIONS_MOBILE_SEARCH", "BUSINESS_CONVERSATIONS", "BUSINESS_BOOKINGS"],
            "dailyRange.startDate.year": start_date.year,
            "dailyRange.startDate.month": start_date.month,
            "dailyRange.startDate.day": start_date.day,
            "dailyRange.endDate.year": end_date.year,
            "dailyRange.endDate.month": end_date.month,
            "dailyRange.endDate.day": end_date.day,
        }
        
        resp = (await run_in_threadpool(lambda: requests.get(url, headers=headers, params=params)))
        
        if not resp.ok:
            raise HTTPException(409 if resp.status_code in (401,403) else 502, 'Google analytics could not be loaded; reconnect or retry')
            
        return {"status": "success", "analytics": resp.json()}
    except Exception as e:
        raise HTTPException(502, 'Google data could not be loaded; reconnect or retry') from e


@app.post("/api/google/search-keywords")
async def get_google_search_keywords(req: GoogleReviewRequest):
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        # Google only returns keyword data for FULLY completed months.
        # We must query the previous month, not the current incomplete month.
        import datetime as dt
        today = dt.datetime.utcnow()
        
        first_day_current = today.replace(day=1)
        prev_month_date = first_day_current - dt.timedelta(days=1)
        start_month_date = prev_month_date - dt.timedelta(days=90) # ~3 months prior
        
        params = {
            "monthlyRange.startMonth.year": start_month_date.year,
            "monthlyRange.startMonth.month": start_month_date.month,
            "monthlyRange.endMonth.year": prev_month_date.year,
            "monthlyRange.endMonth.month": prev_month_date.month,
            "pageSize": 20
        }
        
        clean_loc = req.location_id
        if "locations/" in clean_loc:
            clean_loc = "locations/" + clean_loc.split("locations/")[-1]
        else:
            clean_loc = f"locations/{clean_loc}"
            
        url = f"https://businessprofileperformance.googleapis.com/v1/{clean_loc}/searchkeywords/impressions/monthly"
        resp = (await run_in_threadpool(lambda: requests.get(url, headers=headers, params=params)))
        
        if not resp.ok:
            raise HTTPException(409 if resp.status_code in (401,403) else 502, 'Google data could not be loaded; reconnect or retry')
            
        return {"status": "success", "keywords": resp.json().get("searchKeywordsMonthlyImpressions", [])}
    except Exception as e:
        raise HTTPException(502, 'Google data could not be loaded; reconnect or retry') from e

# ==========================================
# CALENDAR: HYBRID STORAGE SCRUBBER
# ==========================================
from datetime import datetime, timedelta

@app.get("/api/cron/scrub-calendar")
async def scrub_calendar_images():
    """
    CRON JOB ENDPOINT (Runs nightly at midnight)
    Finds all calendar posts that were successfully published yesterday (or older),
    deletes the heavy image file from Supabase Storage to save the 1GB free tier limit,
    but keeps the text caption in the database.
    """
    try:
        today = (datetime.now(timezone.utc)-timedelta(days=30)).date().isoformat()
        
        # 1. Fetch published posts older than today that still have images
        posts = (await run_in_threadpool(lambda: supabase.table('calendar_posts')\
            .select('*')\
            .eq('status', 'published')\
            .lt('post_date', today)\
            .execute()))
            
        deleted_count = 0
        
        for p in posts.data:
            if not p.get('image_url'):
                continue
            registered = await run_in_threadpool(lambda p=p: supabase.table('media_assets').select('id').eq('public_url',p['image_url']).neq('status','deleted').limit(1).execute())
            pending = await run_in_threadpool(lambda p=p: supabase.table('calendar_posts').select('id').eq('image_url',p['image_url']).in_('status',['scheduled','publishing']).limit(1).execute())
            if registered.data or pending.data:
                continue
            # image_url format: https://xyz.supabase.co/storage/v1/object/public/calendar_images/USER_ID/FILENAME.jpg
            # Extract just the "USER_ID/FILENAME.jpg" part
            if 'calendar_images/' in p['image_url']:
                file_path = p['image_url'].split('calendar_images/')[1]
                
                # Delete from storage
                res = await run_in_threadpool(
                    lambda file_path=file_path: supabase.storage.from_('calendar_images').remove([file_path])
                )
                
                # If deleted successfully, set image_url to null in db
                if not getattr(res, 'error', None):
                    (await run_in_threadpool(lambda: supabase.table('calendar_posts').update({'image_url': None}).eq('id', p['id']).execute()))
                    deleted_count += 1
                    
        return {"status": "success", "message": f"Scrubbed {deleted_count} heavy images to save space."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

# ==========================================
# OFFLINE AUTOMATION: GOOGLE OAUTH
# ==========================================

def get_offline_access_token(refresh_token: str) -> str:
    client_id = os.getenv('GOOGLE_CLIENT_ID')
    client_secret = os.getenv('GOOGLE_CLIENT_SECRET')
    if not client_id or not client_secret:
        raise Exception("GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET missing on server.")
        
    url = "https://oauth2.googleapis.com/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }
    resp = requests.post(url, data=payload)
    if not resp.ok:
        raise PermissionError('Google authorization expired; reconnect Google Business Profile')
    return resp.json().get('access_token')

class RefreshTokenRequest(RequestModel):
    user_id: str

@app.post("/api/auth/refresh-google-token")
async def api_refresh_google_token(req: RefreshTokenRequest):
    """
    Called by the frontend Dashboard when the 1-hour Google token expires.
    Fetches the permanent refresh token from Supabase and returns a fresh access token.
    """
    try:
        if not supabase:
            raise HTTPException(503, 'Database is not configured')
            
        user_data = (await run_in_threadpool(lambda: supabase.auth.admin.get_user_by_id(req.user_id)))
        if not user_data.user:
            raise HTTPException(404, 'User not found')
            
        refresh_token = await run_in_threadpool(google_refresh_grant, supabase, user_data.user)
        if not refresh_token:
            raise HTTPException(409, 'Reconnect Google Business Profile')
            
        new_access_token = (await run_in_threadpool(lambda: get_offline_access_token(refresh_token)))
        if not new_access_token:
            raise HTTPException(409, 'Google authorization was rejected; reconnect Google Business Profile')
        return {"status": "success", "provider_token": new_access_token, "expires_in": 3600}
    except HTTPException:
        raise
    except PermissionError as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        raise HTTPException(502, 'Could not refresh Google authorization; reconnect Google if this persists') from None

@app.get("/api/cron/publish-scheduled")
async def publish_scheduled_posts():
    due = datetime.now(timezone.utc).isoformat()
    posts = (await run_in_threadpool(lambda: supabase.table('calendar_posts').select('*').eq('status','scheduled').lte('publish_at',due).execute())).data
    published = 0
    failed = 0
    for post in posts:
        try:
            if datetime.fromisoformat(post['publish_at'].replace('Z','+00:00')) < datetime.now(timezone.utc)-timedelta(hours=1):
                await run_in_threadpool(lambda post=post: supabase.table('calendar_posts').update({'status':'needs_review'}).eq('id',post['id']).eq('status','scheduled').execute())
                continue
            user = (await run_in_threadpool(lambda: supabase.auth.admin.get_user_by_id(post['user_id']))).user
            refresh = await run_in_threadpool(google_refresh_grant, supabase, user)
            if not refresh:
                failed += 1
                continue
            token = (await run_in_threadpool(lambda: get_offline_access_token(refresh)))
            await publish_calendar_post(post['id'], post['user_id'], post['location_id'], token)
            published += 1
        except Exception:
            failed += 1
    campaign_result = await publish_due_campaign_deliveries(due)
    published += campaign_result['published']
    failed += campaign_result['failed']
    if failed:
        raise HTTPException(502, f'{published} posts published; {failed} need attention. Inspect calendar post status and logs.')
    return {'status':'success','published':published,'campaign_deliveries':campaign_result}

@app.get("/api/cron/daily-backlog-reviews")
async def daily_backlog_reviews():
    return await run_review_job()

import base64
import json


async def _resolve_review_automation_rule(user_id: str, location_id: str) -> dict | None:
    specific = (await run_in_threadpool(lambda: supabase.table('review_automation_rules').select('*').eq('user_id', user_id).eq('location_id', location_id).execute())).data or []
    if specific:
        return specific[0]
    defaults = (await run_in_threadpool(lambda: supabase.table('review_automation_rules').select('*').eq('user_id', user_id).is_('location_id', 'null').execute())).data or []
    return defaults[0] if defaults else None


async def _finish_inbound_event(event_id: str, status: str, error: str | None = None):
    await run_in_threadpool(lambda: supabase.table('inbound_events').update({
        'status': status, 'last_error': error, 'processed_at': datetime.now(timezone.utc).isoformat(),
    }).eq('event_id', event_id).execute())

@app.post("/api/webhooks/google-reviews")
async def google_reviews_webhook(req: Request):
    import re
    import hashlib
    body = await req.json()
    encoded = body.get('message', {}).get('data')
    if not encoded: return {'status': 'ignored'}
    try:
        payload = json.loads(base64.b64decode(encoded, validate=True).decode())
        review_name = payload['reviewName']
        match = re.fullmatch(r'accounts/[\w-]+/(locations/[\w-]+)/reviews/[\w-]+', review_name)
        if not match or normalize_location(payload['locationName']) != match.group(1):
            raise ValueError('Resource mismatch')
    except (ValueError, KeyError, UnicodeDecodeError):
        raise HTTPException(400, 'Invalid review notification') from None
    location = match.group(1)
    rows = (await run_in_threadpool(lambda: supabase.table('location_profiles').select('user_id').eq('location_id', location).execute())).data
    if not rows: return {'status': 'ignored'}
    user_id = rows[0]['user_id']
    message_id = body.get('message', {}).get('messageId') or body.get('message', {}).get('message_id')
    event_id = 'google-review:' + (str(message_id) if message_id else hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())
    claimed = (await run_in_threadpool(lambda: supabase.rpc('claim_inbound_event', {
        'p_event': event_id, 'p_user': user_id, 'p_location': location,
        'p_type': 'review.updated', 'p_payload': payload,
    }).execute())).data
    if not claimed:
        return {'status': 'ignored', 'reason': 'Duplicate notification'}
    # Publishing uses a delivery claim; account balances do not govern events.
    await ensure_location_profile(location, user_id)
    user = (await run_in_threadpool(lambda: supabase.auth.admin.get_user_by_id(user_id))).user
    refresh_token = await run_in_threadpool(google_refresh_grant, supabase, user)
    if not refresh_token:
        await _finish_inbound_event(event_id, 'ignored')
        return {'status': 'ignored', 'reason': 'Reconnect Google'}
    try:
        token = (await run_in_threadpool(lambda: get_offline_access_token(refresh_token)))
        response = (await run_in_threadpool(lambda: requests.get(f'https://mybusiness.googleapis.com/v4/{review_name}', headers={'Authorization': f'Bearer {token}'})))
    except Exception as error:
        await _finish_inbound_event(event_id, 'failed', str(error)[:500])
        raise HTTPException(502, 'Google review lookup failed') from error
    if not response.ok:
        await _finish_inbound_event(event_id, 'failed', f'Google review lookup failed ({response.status_code})')
        raise HTTPException(502, 'Google review lookup failed')
    review = response.json()
    if review.get('reviewReply'):
        await _finish_inbound_event(event_id, 'ignored')
        return {'status': 'ignored', 'reason': 'Already replied'}
    rule = await _resolve_review_automation_rule(user_id, location)
    if rule:
        if not rule.get('enabled'):
            await _finish_inbound_event(event_id, 'ignored')
            return {'status': 'ignored', 'reason': 'Automation is disabled for this location'}
        rating = {'ONE': 1, 'TWO': 2, 'THREE': 3, 'FOUR': 4, 'FIVE': 5}.get(review.get('starRating'), 5)
        if not (int(rule['min_rating']) <= rating <= int(rule['max_rating'])):
            await _finish_inbound_event(event_id, 'ignored')
            return {'status': 'ignored', 'reason': 'Outside automation rating range'}
        prompt = (
            f"Write only a concise, natural reply to this Google review. Rating: {rating}/5. "
            f"Comment: {review.get('comment') or '[no written comment]'}. Tone: {rule.get('tone')}. "
            f"Language: {rule.get('language')}. Instructions: {rule.get('custom_instructions') or ''}. "
            "Do not invent facts, include placeholders, or mention these instructions."
        )
        try:
            draft = await run_in_threadpool(generate_ai_reply, prompt)
        except Exception as error:
            await _finish_inbound_event(event_id, 'failed', str(error)[:500])
            raise HTTPException(502, 'AI reply generation failed') from error
        mode = rule.get('mode') or 'draft'
        blocked_terms = [
            str(term).strip().casefold() for term in (rule.get('blocked_terms') or [])
            if str(term).strip()
        ]
        safety_requires_approval = any(
            term in (review.get('comment') or '').casefold() or term in draft.casefold()
            for term in blocked_terms
        )
        daily_limit_reached = False
        # The database enforces this atomically at publication, in the account's
        # business timezone, rather than counting UTC draft-creation dates here.
        status = (
            'draft' if mode == 'draft' else
            'pending_approval' if mode == 'approval' or safety_requires_approval or daily_limit_reached else
            'scheduled'
        )
        scheduled_for = datetime.now(timezone.utc) + timedelta(minutes=int(rule.get('delay_minutes') or 0)) if status == 'scheduled' else None
        job = {
            'event_id': event_id, 'user_id': user_id, 'location_id': location,
            'rule_id': rule['id'], 'review_name': review_name, 'rating': rating,
            'review_text': review.get('comment') or '', 'draft_text': draft.strip()[:4096],
            'status': status, 'scheduled_for': scheduled_for.isoformat() if scheduled_for else None,
            'last_error': (
                'Manual approval required because a blocked term matched'
                if safety_requires_approval else
                'Daily automatic-publishing limit reached; manual approval required'
                if daily_limit_reached else None
            ),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        saved_job = await run_in_threadpool(lambda: supabase.rpc('enqueue_review_reply', {'p_job':job}).execute().data)
        saved = [saved_job] if saved_job else []
        await _finish_inbound_event(event_id, 'completed')
        if status == 'scheduled' and int(rule.get('delay_minutes') or 0) == 0 and saved:
            delivery = await publish_due_review_reply_jobs(job_id=saved[0]['id'], limit=1)
            return {'status': 'queued', 'mode': mode, 'job_id': saved[0]['id'], 'delivery': delivery}
        return {'status': status, 'mode': mode, 'job_id': saved[0]['id'] if saved else None, 'draft_reply': draft}
    settings = await get_ai_settings(GetAISettingsRequest(user_id=user_id, location_id=location))
    if not settings.get('is_ai_active', True) or (review.get('starRating') in ('ONE','TWO') and not settings.get('reply_to_1_star', False)):
        await _finish_inbound_event(event_id, 'ignored')
        return {'status': 'ignored', 'reason': 'Disabled in settings'}
    try:
        result = await generate_and_publish(user_id, location, token, review, settings)
        await _finish_inbound_event(event_id, 'completed')
        return result
    except HTTPException as error:
        if error.status_code == 409:
            await _finish_inbound_event(event_id, 'ignored')
            return {'status': 'ignored', 'reason': error.detail}
        await _finish_inbound_event(event_id, 'failed', str(error.detail)[:500])
        raise

# ==========================================
# COMPETITORS API
# ==========================================

class CompetitorRequest(RequestModel):
    user_id: str
    location_name: str
    keyword: str

@app.post("/api/google/competitors")
async def get_competitors(req: CompetitorRequest):
    """
    Finds local competitors and their top reviews using the Google Places API.
    Uses a fallback if GOOGLE_MAPS_API_KEY is not set.
    """
    maps_key = os.getenv('GOOGLE_MAPS_API_KEY')
    
    if not maps_key:
        raise HTTPException(503, 'Competitor lookup is not configured')

    try:
        search_url = f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={req.keyword} near {req.location_name}&key={maps_key}"
        resp = (await run_in_threadpool(lambda: requests.get(search_url)))
        if not resp.ok:
            raise HTTPException(status_code=500, detail="Google Places API failed")
            
        data = resp.json()
        places = data.get('results', [])[:3] # Top 3 competitors
        
        competitors = []
        for p in places:
            # Fetch details to get the top text review
            place_id = p.get('place_id')
            details_url = f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&fields=name,rating,user_ratings_total,reviews&key={maps_key}"
            det_resp = (await run_in_threadpool(lambda: requests.get(details_url)))
            top_review = ""
            
            if det_resp.ok:
                det_data = det_resp.json().get('result', {})
                reviews = det_data.get('reviews', [])
                if reviews:
                    top_review = reviews[0].get('text', '')[:120] + "..." # Truncate long reviews
                    
            competitors.append({
                "name": p.get('name'),
                "rating": p.get('rating', 0),
                "user_ratings_total": p.get('user_ratings_total', 0),
                "top_review": top_review
            })
            
        return {"status": "success", "competitors": competitors}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail="Upstream operation failed; please retry") from e

# ─── User Profile & Onboarding ───

@app.post("/api/user/profile")
async def get_user_profile(req: UserProfileRequest):
    profile = await ensure_user_profile(req.user_id)
    if req.location_id:
        location = await ensure_location_profile(req.location_id, req.user_id)
        profile = {**profile, **location, 'max_seo_keywords': None if location.get('unlimited_access') else PRICING_PLANS[location['plan_type']]['max_keywords']}
    return profile


@app.post('/api/user/reset-onboarding')
async def reset_onboarding(req: UserProfileRequest):
    (await run_in_threadpool(lambda: supabase.table('user_profiles').update({'onboarding_completed': False}).eq('id',req.user_id).execute()))
    return {'status': 'success'}

@app.post("/api/user/onboarding")
async def save_onboarding(req: OnboardingRequest, request: Request):
    user_id = request.state.user.id
    profile = await ensure_user_profile(user_id)
    
    # Validate keyword count (free plan = max 2)
    plan = PRICING_PLANS.get(profile.get('plan_type', 'free'), PRICING_PLANS['free'])
    max_kw = None if profile.get('unlimited_access') else plan['max_keywords']
    keywords = req.seo_keywords[:max_kw]  # Trim to allowed count
    
    update_data = {
        'reply_length': req.reply_length,
        'seo_keywords': keywords,
        'onboarding_completed': True,
        'updated_at': datetime.utcnow().isoformat()
    }
    if req.full_name:
        update_data['full_name'] = req.full_name
    
    (await run_in_threadpool(lambda: supabase.table('user_profiles').update(update_data).eq('id', user_id).execute()))
    
    return {'status': 'success', 'keywords_saved': len(keywords), 'max_allowed': max_kw}

# ─── Batch Review Reply ───

@app.post("/api/reviews/batch-reply")
async def batch_reply_reviews(req: BatchReplyRequest):
    profile = await ensure_location_profile(req.location_id, req.user_id)
    headers = {'Authorization': f'Bearer {req.access_token}'}
    settings = await get_ai_settings(GetAISettingsRequest(user_id=req.user_id, location_id=req.location_id))
    if not settings.get('is_ai_active', True):
        return {'status': 'success', 'replied': [], 'message': 'AI is disabled for this location'}
    all_reviews, next_page = await scan_google_reviews(profile['account_id'], req.location_id, req.access_token)
    reviews = [r for r in all_reviews if not r.get('reviewReply')]
    if not settings.get('reply_to_1_star', False):
        reviews = [r for r in reviews if r.get('starRating') not in ('ONE','TWO')]
    replies = []
    for review in reviews[:req.count]:
        try:
            result = await generate_and_publish(req.user_id, req.location_id, req.access_token, review, settings)
            replies.append({'review_name': review['name'], **result})
        except HTTPException as error:
            replies.append({'review_name': review['name'], 'status': 'failed', 'error': error.detail})
    return {'status': 'success', 'replied': replies, 'more_backlog': bool(next_page)}


async def scan_google_reviews(account, location, token, max_pages=5):
    """Bounded traversal, reporting continuation instead of hiding older pages."""
    reviews, page, seen = [], None, set()
    for _ in range(max_pages):
        params = {'pageSize':50,'orderBy':'updateTime desc'}
        if page:
            params['pageToken'] = page
        response = await run_in_threadpool(lambda: requests.get(
            f'https://mybusiness.googleapis.com/v4/{account}/{location}/reviews',
            headers={'Authorization':f'Bearer {token}'},params=params))
        if not response.ok:
            raise HTTPException(502, 'Could not scan Google reviews')
        data = response.json()
        reviews.extend(data.get('reviews', []))
        page = data.get('nextPageToken')
        if not page:
            break
        if page in seen:
            raise HTTPException(502, 'Google repeated a pagination token; scan stopped safely')
        seen.add(page)
    return reviews, page


async def poll_rule_reviews(user_id, location, token):
    """Webhook fallback uses the same validation, queue and deduplication path."""
    from types import SimpleNamespace
    import hashlib
    account = await run_in_threadpool(registered_account, location)
    reviews, next_page = await scan_google_reviews(account, location, token, max_pages=2)
    published = failed = 0
    for review in [item for item in reviews if not item.get('reviewReply')][:10]:
        name = review.get('name')
        if not name:
            continue
        key = hashlib.sha256((name+str(review.get('updateTime',''))).encode()).hexdigest()
        payload = {'reviewName':name,'locationName':location}
        async def notification(payload=payload, key=key):
            return {'message':{'messageId':'poll:'+key,'data':base64.b64encode(json.dumps(payload).encode()).decode()}}
        try:
            result = await google_reviews_webhook(SimpleNamespace(json=notification))
            published += result.get('delivery',{}).get('published',0)
            failed += result.get('delivery',{}).get('failed',0)
        except Exception:
            failed += 1
    return {'published':published,'failed':failed,'more_backlog':bool(next_page)}

@app.post("/api/reviews/regenerate-reply")
async def regenerate_reply(req: RegenerateReplyRequest):
    settings = await get_ai_settings(GetAISettingsRequest(user_id=req.user_id, location_id=req.location_id))
    review = {'name': req.review_name, 'comment': req.review_text, 'starRating': req.star_rating}
    return await generate_and_publish(req.user_id, req.location_id, req.access_token, review, settings, regenerate=True)

# ─── Rank Report ───

@app.post("/api/rank/generate-report")
async def generate_rank_report(req: RankReportRequest):
    await ensure_location_profile(req.location_id, req.user_id)
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', req.request_id):
        raise HTTPException(422, 'request_id may contain only letters, numbers, underscores, and hyphens')
    from report_cache import measured_report
    async def token_getter():
        user = (await run_in_threadpool(lambda: supabase.auth.admin.get_user_by_id(req.user_id))).user
        refresh = await run_in_threadpool(google_refresh_grant, supabase, user) if user else None
        if not refresh:
            raise HTTPException(409, 'Reconnect Google Business Profile before generating a rank report')
        return await run_in_threadpool(get_offline_access_token, refresh)
    try:
        report = await measured_report(supabase, req.user_id, req.location_id, req.keyword, req.request_id, token_getter, run_local_rank_scan)
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        raise HTTPException(502, 'Rank report could not be generated; retry with the same request ID') from None
    signing_secret = os.getenv('RANK_REPORT_SIGNING_SECRET') or os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
    pdf_token, pdf_expires_at = create_rank_pdf_token(report, signing_secret)
    api_base = os.getenv('PUBLIC_API_URL', 'https://gbp-auto-master-backend-us.onrender.com').rstrip('/')
    return {'status':'success', **report,
            'pdf_download_url': f'{api_base}/api/rank/report.pdf?token={pdf_token}',
            'pdf_expires_at': datetime.fromtimestamp(pdf_expires_at, timezone.utc).isoformat()}




async def location_subscriptions(user_id: str) -> dict:
    rows = (await run_in_threadpool(lambda: supabase.table('location_profiles').select('*').eq('user_id', user_id).execute())).data
    result = {}
    for row in rows:
        profile = await ensure_location_profile(row['location_id'], user_id)
        if profile['plan_type'] != 'free':
            result[row['location_id']] = {'status': 'active', 'plan_id': profile['plan_type'], 'expires_at': profile['subscription_end'], 'is_lifetime': profile.get('is_lifetime', False), 'unlimited_access': profile.get('unlimited_access', False)}
    return result


def reserve_operation(location_id, user_id, operation_id):
    result = supabase.rpc('claim_operation', {'p_operation': operation_id, 'p_location': normalize_location(location_id) if location_id else None, 'p_user': user_id}).execute().data
    if not result['success']:
        raise HTTPException(409, result.get('error', 'Operation is already in progress or completed'))
    return result


def finish_operation(operation_id, success):
    supabase.rpc('finish_operation', {'p_operation': operation_id, 'p_success': success}).execute()


def registered_account(location_id):
    rows = supabase.table('location_profiles').select('account_id').eq('location_id', normalize_location(location_id)).execute().data
    if not rows: raise HTTPException(409, 'Reconnect Google locations')
    return rows[0]['account_id']


async def generate_and_publish(user_id, location_id, token, review, settings, regenerate=False):
    import re
    name = review.get('name', '')
    profile = await ensure_location_profile(location_id, user_id)
    if not re.fullmatch(re.escape(profile['account_id']+'/'+normalize_location(location_id)) + r'/reviews/[\w-]+', name):
        raise HTTPException(403, 'Review does not belong to this location')
    operation = ('regenerate:' + uuid4().hex) if regenerate else 'reply:' + name
    (await run_in_threadpool(lambda: reserve_operation(location_id, user_id, operation)))
    try:
        prompt = f"Write only a professional reply to this review: {review.get('comment','')} ({review.get('starRating','')}). Tone: {settings.get('ai_tone','Professional')}. Keywords to use naturally: {settings.get('active_keywords', [])}. Instructions: {settings.get('custom_instructions','')}. Do not invent facts."
        reply = (await run_in_threadpool(lambda: generate_ai_reply(prompt)))
        if not reply: raise HTTPException(502, 'AI returned no reply')
    except Exception:
        (await run_in_threadpool(lambda: finish_operation(operation, False)))
        raise
    # On an ambiguous network failure, retain the reservation for reconciliation;
    # refunding and blindly retrying could duplicate a reply that Google accepted.
    try:
        response = (await run_in_threadpool(lambda: requests.put(f'https://mybusiness.googleapis.com/v4/{name}/reply', headers={'Authorization':f'Bearer {token}'}, json={'comment':reply})))
    except Exception:
        raise HTTPException(502, 'Google delivery is uncertain; the reply is held for reconciliation') from None
    (await run_in_threadpool(lambda: finish_operation(operation, response.ok)))
    if not response.ok: raise HTTPException(502, 'Google rejected the reply')
    return {'status':'published','ai_reply':reply}


class SchedulePostRequest(RequestModel):
    user_id: str
    location_id: str
    post_date: date
    caption: str = Field(default='', max_length=1500)
    image_url: str | None = None
    post_type: str = 'LOCAL_POST'


class CalendarPostRequest(RequestModel):
    user_id: str
    location_id: str
    post_id: str
    provider_token: str | None = None


@app.post('/api/calendar/schedule')
async def schedule_calendar_post(req: SchedulePostRequest):
    if req.post_type not in ('LOCAL_POST','PHOTO','VIDEO'):
        raise HTTPException(422, 'Invalid post type')
    if not req.caption.strip() and not req.image_url:
        raise HTTPException(422, 'Add text or media')
    if req.post_type in ('PHOTO','VIDEO') and not req.image_url:
        raise HTTPException(422, 'Media URL required')
    return (await run_in_threadpool(lambda: supabase.rpc('schedule_post', {'p_user':req.user_id,'p_location':req.location_id,'p_date':req.post_date.isoformat(),
        'p_caption':req.caption,'p_image':req.image_url,'p_type':req.post_type}).execute())).data


@app.post('/api/calendar/delete')
async def delete_calendar_post(req: CalendarPostRequest):
    rows = (await run_in_threadpool(lambda: supabase.table('calendar_posts').delete().eq('id',req.post_id).eq('user_id',req.user_id).eq('location_id',req.location_id).eq('status','scheduled').execute())).data
    if not rows: raise HTTPException(409, 'Only pending scheduled posts can be deleted')
    return {'status':'success'}


@app.post('/api/calendar/publish')
async def publish_calendar_endpoint(req: CalendarPostRequest):
    if not req.provider_token: raise HTTPException(422, 'Google connection required')
    return await publish_calendar_post(req.post_id, req.user_id, req.location_id, req.provider_token)


async def publish_calendar_post(post_id, user_id, location_id, token):
    await ensure_location_profile(location_id,user_id)
    # Claim the row before calling Google. An uncertain delivery stays publishing.
    rows = (await run_in_threadpool(lambda: supabase.table('calendar_posts').update({'status':'publishing'}).eq('id',post_id).eq('user_id',user_id).eq('location_id',location_id).eq('status','scheduled').execute())).data
    if not rows: raise HTTPException(409, 'Post is already published or being processed')
    post = rows[0]
    result = await publish_local_post(PublishPostRequest(provider_token=token,location_id=location_id,
        summary=post.get('caption',''),image_url=post.get('image_url'),post_type=post.get('post_type','LOCAL_POST')))
    if result.get('status') != 'success':
        (await run_in_threadpool(lambda: supabase.table('calendar_posts').update({'status':'failed'}).eq('id',post_id).execute()))
        raise HTTPException(502, 'Google rejected this post; inspect it before retrying')
    (await run_in_threadpool(lambda: supabase.table('calendar_posts').update({'status':'published'}).eq('id',post_id).execute()))
    return {'status':'success'}


def _google_event_part(value: str) -> tuple[dict, dict]:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (
        {'year': parsed.year, 'month': parsed.month, 'day': parsed.day},
        {'hours': parsed.hour, 'minutes': parsed.minute, 'seconds': parsed.second},
    )


def _campaign_google_payload(campaign: dict, media: dict | None) -> dict:
    topic_type = campaign.get('topic_type') or 'STANDARD'
    payload = {
        'languageCode': campaign.get('language_code') or 'en',
        'summary': campaign.get('summary') or '',
        'topicType': topic_type,
    }
    call_to_action = campaign.get('call_to_action')
    if isinstance(call_to_action, dict) and call_to_action.get('action_type'):
        payload['callToAction'] = {
            'actionType': call_to_action['action_type'],
            **({'url': call_to_action['url']} if call_to_action.get('url') else {}),
        }
    if topic_type in ('EVENT', 'OFFER'):
        event = campaign.get('event_details') or {}
        if not all(event.get(key) for key in ('title', 'start_time', 'end_time')):
            raise ValueError('Campaign event details are incomplete')
        start_date, start_time = _google_event_part(event['start_time'])
        end_date, end_time = _google_event_part(event['end_time'])
        payload['event'] = {
            'title': event['title'],
            'schedule': {
                'startDate': start_date, 'startTime': start_time,
                'endDate': end_date, 'endTime': end_time,
            },
        }
    if topic_type == 'OFFER':
        offer = campaign.get('offer_details') or {}
        payload['offer'] = {
            **({'couponCode': offer['coupon_code']} if offer.get('coupon_code') else {}),
            **({'redeemOnlineUrl': offer['redeem_online_url']} if offer.get('redeem_online_url') else {}),
            **({'termsConditions': offer['terms_conditions']} if offer.get('terms_conditions') else {}),
        }
    if media:
        if media.get('media_kind') != 'photo':
            raise ValueError('Video attachments are not enabled for local posts yet')
        if not media.get('public_url'):
            raise ValueError('Campaign media does not have a public URL')
        payload['media'] = [{'mediaFormat': 'PHOTO', 'sourceUrl': media['public_url']}]
    return payload


async def _refresh_campaign_status(campaign_id: str):
    deliveries = (await run_in_threadpool(lambda: supabase.table('campaign_locations').select('status').eq('campaign_id', campaign_id).execute())).data or []
    statuses = {row['status'] for row in deliveries}
    if statuses == {'published'}:
        status = 'published'
    elif 'published' in statuses and statuses.issubset({'published', 'failed'}):
        status = 'partially_published'
    elif statuses == {'failed'}:
        status = 'failed'
    elif 'publishing' in statuses:
        status = 'processing'
    else:
        status = 'scheduled'
    await run_in_threadpool(lambda: supabase.table('content_campaigns').update({'status': status, 'updated_at': datetime.now(timezone.utc).isoformat()}).eq('id', campaign_id).execute())


async def publish_due_campaign_deliveries(due: str | None = None) -> dict:
    due = due or datetime.now(timezone.utc).isoformat()
    deliveries = (await run_in_threadpool(lambda: supabase.table('campaign_locations').select('*').eq('status', 'scheduled').lte('scheduled_for', due).limit(100).execute())).data or []
    published = failed = uncertain = 0
    for delivery in deliveries:
        claimed = await run_in_threadpool(lambda delivery=delivery: supabase.rpc('claim_campaign_delivery', {
            'p_campaign': delivery['campaign_id'], 'p_location': delivery['location_id'],
        }).execute().data)
        if not claimed:
            continue
        delivery_started = False
        try:
            campaigns = (await run_in_threadpool(lambda delivery=delivery: supabase.table('content_campaigns').select('*').eq('id', delivery['campaign_id']).eq('user_id', delivery['user_id']).execute())).data or []
            if not campaigns:
                raise ValueError('Campaign not found')
            campaign = campaigns[0]
            media_id = delivery.get('media_asset_id') or campaign.get('media_asset_id')
            media = None
            if media_id:
                media_rows = (await run_in_threadpool(lambda media_id=media_id, delivery=delivery: supabase.table('media_assets').select('*').eq('id', media_id).eq('user_id', delivery['user_id']).execute())).data or []
                if not media_rows:
                    raise ValueError('Campaign media not found')
                media = media_rows[0]
            user = (await run_in_threadpool(lambda delivery=delivery: supabase.auth.admin.get_user_by_id(delivery['user_id']))).user
            refresh = await run_in_threadpool(google_refresh_grant, supabase, user)
            if not refresh:
                raise PermissionError('Google connection requires authorization')
            token = await run_in_threadpool(get_offline_access_token, refresh)
            account_id = await run_in_threadpool(registered_account, delivery['location_id'])
            url = f"https://mybusiness.googleapis.com/v4/{account_id}/{delivery['location_id']}/localPosts"
            payload = _campaign_google_payload(campaign, media)
            delivery_started = True
            try:
                response = await run_in_threadpool(lambda: requests.post(url, headers={'Authorization': f'Bearer {token}'}, json=payload))
            except Exception:
                uncertain += 1
                await run_in_threadpool(lambda delivery=delivery: supabase.table('campaign_locations').update({
                    'last_error': 'Google delivery is uncertain; reconcile before retrying',
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                }).eq('campaign_id', delivery['campaign_id']).eq('location_id', delivery['location_id']).eq('status', 'publishing').execute())
                await _refresh_campaign_status(delivery['campaign_id'])
                continue
            if not response.ok:
                if response.status_code >= 500 or response.status_code == 408:
                    raise RuntimeError('Google delivery is uncertain')
                failed += 1
                await run_in_threadpool(lambda delivery=delivery, response=response: supabase.table('campaign_locations').update({
                    'status': 'failed', 'last_error': f'Google rejected the post ({response.status_code})',
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                }).eq('campaign_id', delivery['campaign_id']).eq('location_id', delivery['location_id']).eq('status', 'publishing').execute())
            else:
                data = response.json()
                published += 1
                await run_in_threadpool(lambda delivery=delivery, data=data: supabase.table('campaign_locations').update({
                    'status': 'published', 'google_post_name': data.get('name'),
                    'google_search_url': data.get('searchUrl'), 'published_at': datetime.now(timezone.utc).isoformat(),
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                }).eq('campaign_id', delivery['campaign_id']).eq('location_id', delivery['location_id']).eq('status', 'publishing').execute())
            await _refresh_campaign_status(delivery['campaign_id'])
        except Exception as error:
            # After dispatch, even a JSON/DB/audit error is uncertain. Keep the
            # delivery held so another worker cannot republish accepted content.
            if delivery_started:
                uncertain += 1
            else:
                failed += 1
            await run_in_threadpool(lambda delivery=delivery, error=error: supabase.table('campaign_locations').update({
                'status': 'publishing' if delivery_started else 'failed',
                'last_error': 'Delivery needs reconciliation; inspect Google before retrying' if delivery_started else 'Preparation failed; inspect server logs',
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('campaign_id', delivery['campaign_id']).eq('location_id', delivery['location_id']).eq('status', 'publishing').execute())
            await _refresh_campaign_status(delivery['campaign_id'])
    return {'published': published, 'failed': failed, 'uncertain': uncertain}


async def publish_due_review_reply_jobs(
    due: str | None = None, job_id: str | None = None, limit: int = 100
) -> dict:
    due = due or datetime.now(timezone.utc).isoformat()
    query = supabase.table('review_reply_jobs').select('*').eq('status', 'scheduled').lte('scheduled_for', due)
    if job_id:
        query = query.eq('id', job_id)
    jobs = (await run_in_threadpool(lambda: query.limit(limit).execute())).data or []
    published = failed = uncertain = 0
    for job in jobs:
        claimed = await run_in_threadpool(lambda job=job: supabase.rpc('claim_review_delivery', {'p_job': job['id']}).execute().data)
        if not claimed:
            continue
        operation = f"auto-reply:{job['id']}"
        reservation = (await run_in_threadpool(lambda job=job, operation=operation: supabase.rpc('claim_operation', {
            'p_operation': operation, 'p_location': job['location_id'],
            'p_user': job['user_id'],
        }).execute())).data or {}
        if not reservation.get('success'):
            failed += 1
            await run_in_threadpool(lambda job=job, reservation=reservation: supabase.table('review_reply_jobs').update({
                'status': 'failed', 'last_error': reservation.get('error') or 'Operation is already in progress or completed',
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', job['id']).eq('status', 'publishing').execute())
            continue
        delivery_started = False
        try:
            user = (await run_in_threadpool(lambda job=job: supabase.auth.admin.get_user_by_id(job['user_id']))).user
            refresh = await run_in_threadpool(google_refresh_grant, supabase, user)
            if not refresh:
                raise PermissionError('Google connection requires authorization')
            token = await run_in_threadpool(get_offline_access_token, refresh)
            delivery_started = True
            try:
                response = await run_in_threadpool(lambda job=job, token=token: requests.put(
                    f"https://mybusiness.googleapis.com/v4/{job['review_name']}/reply",
                    headers={'Authorization': f'Bearer {token}'}, json={'comment': job['draft_text']},
                ))
            except Exception:
                uncertain += 1
                await run_in_threadpool(lambda job=job: supabase.table('review_reply_jobs').update({
                    'last_error': 'Google delivery is uncertain; reconcile before retrying',
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                }).eq('id', job['id']).eq('status', 'publishing').execute())
                continue
            await run_in_threadpool(lambda operation=operation, response=response: supabase.rpc('finish_operation', {
                'p_operation': operation, 'p_success': response.ok,
            }).execute())
            if not response.ok:
                failed += 1
                await run_in_threadpool(lambda job=job, response=response: supabase.table('review_reply_jobs').update({
                    'status': 'failed', 'last_error': f'Google rejected the reply ({response.status_code})',
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                }).eq('id', job['id']).eq('status', 'publishing').execute())
                continue
            published += 1
            await run_in_threadpool(lambda job=job: supabase.table('review_reply_jobs').update({
                'status': 'published', 'published_at': datetime.now(timezone.utc).isoformat(),
                'credits_charged': 0, 'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', job['id']).eq('status', 'publishing').execute())
            await run_in_threadpool(lambda job=job: supabase.table('audit_events').insert({
                'user_id': job['user_id'], 'location_id': job['location_id'], 'actor_type': 'automation',
                'action': 'publish_reply', 'resource_type': 'google_review',
                'resource_id': job['review_name'], 'detail': {},
            }).execute())
        except Exception as error:
            if not delivery_started:
                await run_in_threadpool(lambda operation=operation: supabase.rpc('finish_operation', {
                    'p_operation': operation, 'p_success': False,
                }).execute())
                failed += 1
            else:
                uncertain += 1
            await run_in_threadpool(lambda job=job, error=error: supabase.table('review_reply_jobs').update({
                'status': 'publishing' if delivery_started else 'failed',
                'last_error': 'Delivery needs reconciliation; inspect Google before retrying' if delivery_started else 'Preparation failed; inspect server logs',
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', job['id']).eq('status', 'publishing').execute())
    return {'published': published, 'failed': failed, 'uncertain': uncertain}


@app.post('/api/google/publish-post')
async def publish_immediate_post(req: PublishPostRequest, request: Request):
    owner = (await run_in_threadpool(lambda: supabase.table('location_profiles').select('user_id').eq('location_id',req.location_id).single().execute())).data['user_id']
    if not req.request_id:
        raise HTTPException(422, 'Provide a stable request_id and reuse it when retrying')
    operation = 'publish:' + owner + ':' + req.request_id
    import hashlib
    fingerprint = hashlib.sha256(json.dumps(req.model_dump(exclude={'provider_token','request_id'}),sort_keys=True).encode()).hexdigest()
    claim = await run_in_threadpool(lambda: supabase.rpc('claim_result_operation', {
        'p_operation':operation,'p_location':req.location_id,'p_user':owner,'p_fingerprint':fingerprint,
    }).execute().data)
    if not claim.get('success'):
        raise HTTPException(409, claim.get('error','Operation is already running'))
    if claim.get('replayed'):
        return claim['result']
    result = await publish_local_post(req)
    if result.get('status') == 'success':
        await run_in_threadpool(lambda: supabase.rpc('complete_result_operation', {'p_operation':operation,'p_result':result}).execute())
    else:
        await run_in_threadpool(finish_operation, operation, False)
        raise HTTPException(502, 'Google rejected this post; inspect it before trying again')
    return result


def list_all_users():
    users = []
    page = 1
    while True:
        batch = supabase.auth.admin.list_users(page=page, per_page=100)
        users.extend(batch)
        if len(batch)<100: return users
        page += 1


async def run_review_job():
    users = (await run_in_threadpool(list_all_users))
    sent = 0
    failed = 0
    for user in users:
        subscriptions = await location_subscriptions(user.id)
        try:
            refresh = await run_in_threadpool(google_refresh_grant, supabase, user)
            if not subscriptions or not refresh:
                continue
            token = (await run_in_threadpool(get_offline_access_token, refresh))
        except Exception:
            failed += 1
            continue
        for location in subscriptions:
            # V2 webhook automation owns only the locations covered by a rule.
            # Do not let one configured location disable legacy replies for all
            # other locations on the same Google account.
            rule = await _resolve_review_automation_rule(user.id, location)
            if rule:
                if rule.get('enabled'):
                    try:
                        result = await poll_rule_reviews(user.id, location, token)
                        sent += result['published']
                        failed += result['failed']
                    except Exception:
                        failed += 1
                continue
            try:
                result = await sync_and_reply_reviews(GoogleReviewRequest(user_id=user.id, location_id=location, provider_token=token))
                sent += sum(1 for reply in result.get('replied',[]) if reply['status']=='published')
                failed += len(result.get('failures',[]))
            except Exception:
                failed += 1
    if failed:
        raise HTTPException(502, f'{sent} replies sent; {failed} operations need attention. Inspect operation_runs and Google connections.')
    return {'status':'success','message':f'AI sent {sent} replies'}


# Build MCP only after the legacy helpers it reuses have been defined.
gbp_mcp = create_gbp_mcp_server(
    supabase,
    google_token_getter=get_offline_access_token,
    ai_reply_generator=generate_ai_reply,
)
gbp_mcp_http = gbp_mcp.streamable_http_app(
    transport_security=mcp_transport_security(),
)

# Keep this catch-all mount last. Starlette evaluates routes in declaration order,
# so every existing HTTP API route above remains reachable and /mcp is handled by
# the official MCP Streamable HTTP application.
app.mount("/", gbp_mcp_http)
