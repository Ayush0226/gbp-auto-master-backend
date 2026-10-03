import os
import razorpay
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local", override=False)

from security import authorize, normalize_location, track_job
import billing
from mcp_auth import protected_resource_metadata
from uuid import uuid4
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool
from mcp_server import create_gbp_mcp_server, mcp_transport_security


@asynccontextmanager
async def lifespan(application):
    required = ('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'RAZORPAY_KEY_ID',
                'RAZORPAY_KEY_SECRET', 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GROQ_API_KEY')
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError('Missing environment variables: ' + ', '.join(missing))
    try:
        (await run_in_threadpool(lambda: application.state.db.table('account_token_ledger').select('id').limit(0).execute()))
    except Exception:
        raise RuntimeError('Database migration 001 must be applied before starting this backend') from None
    try:
        (await run_in_threadpool(lambda: application.state.db.table('calendar_posts').select('publish_at').limit(0).execute()))
    except Exception:
        raise RuntimeError('Database migration 002 must be applied before starting this backend') from None
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

# Initialize Razorpay Client
razorpay_client = razorpay.Client(auth=(os.getenv('RAZORPAY_KEY_ID', ''), os.getenv('RAZORPAY_KEY_SECRET', '')))

# Initialize Supabase Client
supabase_url = os.getenv('SUPABASE_URL', '')
supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')

# Only initialize if keys are present
if supabase_url and supabase_key:
    supabase: Client = create_client(supabase_url, supabase_key)
else:
    supabase = None

app.state.db = supabase


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
async def oauth_protected_resource_metadata():
    """Advertise the Supabase authorization server to MCP clients."""
    return protected_resource_metadata()

class RequestModel(BaseModel):
    @field_validator('location_id', check_fields=False)
    @classmethod
    def canonical_location(cls, value):
        return normalize_location(value) if value is not None else None

PRICING_PLANS = {
    'free':        {'price': 0,    'tokens_monthly': 0,  'max_keywords': 2,  'competitor': False, 'duration_months': 0},
    'monthly':     {'price': 500,  'tokens_monthly': 350, 'max_keywords': 5,  'competitor': False, 'duration_months': 1},
    'half_yearly': {'price': 2500, 'tokens_monthly': 600, 'max_keywords': 10, 'competitor': True,  'duration_months': 6},
    'yearly':      {'price': 5000, 'tokens_monthly': 750, 'max_keywords': 15, 'competitor': True,  'duration_months': 12},
}

TOP_UP_PACKS = {
    'standard': {'price': 500, 'tokens': 450, 'name': 'Standard Top-Up'},
    'bulk':     {'price': 900, 'tokens': 1000, 'name': 'Bulk Top-Up'},
}

class OrderRequest(RequestModel):
    plan_id: str
    promo_code: str
    user_id: str # Supabase User ID
    location_id: str

class OnboardingRequest(RequestModel):
    user_id: str
    reply_length: str  # '10-40', '50-90', '100-120'
    seo_keywords: list[str]
    full_name: str = None

class DailyClaimRequest(RequestModel):
    user_id: str
    location_id: str

class TokenBalanceRequest(RequestModel):
    user_id: str
    location_id: str | None = None

class PromoCodeRequest(RequestModel):
    user_id: str
    location_id: str | None = None
    promo_code: str

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
    keyword: str
    location_id: str
    access_token: str

class UserProfileRequest(RequestModel):
    user_id: str
    location_id: str | None = None

class TopUpRequest(RequestModel):
    user_id: str
    location_id: str | None = None
    pack_id: str = 'standard'
    promo_code: str = ''

# ─── Token System Helpers ───
from datetime import date, datetime, timedelta, timezone

async def get_token_balance(location_id: str | None, user_id: str) -> float:
    # All Google profiles draw from the same signed-in account balance.
    profile = await ensure_user_profile(user_id)
    return float(profile.get('tokens_balance') or 0)

async def deduct_tokens(location_id: str, user_id: str, amount: float, action: str, description: str = '', reference_id: str = '') -> dict:
    return (await run_in_threadpool(lambda: supabase.rpc('change_tokens', {'p_location': normalize_location(location_id) if location_id else None, 'p_user': user_id, 'p_amount': -amount, 'p_action': action, 'p_description': description, 'p_reference': reference_id or None}).execute())).data

async def credit_tokens(location_id: str, user_id: str, amount: float, action: str, description: str = '') -> float:
    result = (await run_in_threadpool(lambda: supabase.rpc('change_tokens', {'p_location': normalize_location(location_id) if location_id else None, 'p_user': user_id, 'p_amount': amount, 'p_action': action, 'p_description': description, 'p_reference': None}).execute())).data
    return result['balance']

async def ensure_location_profile(location_id: str, user_id: str) -> dict:
    (await run_in_threadpool(lambda: supabase.rpc('refresh_monthly_tokens', {'p_location': normalize_location(location_id), 'p_user': user_id}).execute()))
    rows = (await run_in_threadpool(lambda: supabase.table('location_profiles').select('*').eq('location_id', normalize_location(location_id)).eq('user_id', user_id).execute())).data
    if not rows:
        raise HTTPException(409, 'Reconnect Google locations before continuing')
    profile = rows[0]
    end = profile.get('subscription_end')
    if profile.get('plan_type') != 'free' and (not end or datetime.fromisoformat(end.replace('Z', '+00:00')).timestamp() <= datetime.now().timestamp()):
        profile['plan_type'] = 'free'
    return profile

async def ensure_user_profile(user_id: str) -> dict:
    return (await run_in_threadpool(lambda: supabase.rpc('ensure_account', {'p_user': user_id}).execute())).data

@app.post("/api/payment/create-order")
async def create_order(req: OrderRequest):
    if req.plan_id not in PRICING_PLANS or req.plan_id == 'free':
        raise HTTPException(400, 'Select a paid plan; the free plan is already available')
    return (await run_in_threadpool(lambda: billing.create_order(supabase, razorpay_client, req.user_id, req.location_id, 'subscription', req.plan_id, PRICING_PLANS[req.plan_id], req.promo_code)))

class VerifyRequest(RequestModel):
    razorpay_payment_id: str
    razorpay_order_id: str
    razorpay_signature: str
    user_id: str
    location_id: str | None = None
    plan_id: str = None

@app.post("/api/payment/verify")
async def verify_payment(req: VerifyRequest):
    try:
        return (await run_in_threadpool(lambda: billing.verify_order(supabase, razorpay_client, req, 'subscription')))
    except razorpay.errors.SignatureVerificationError:
        raise HTTPException(400, 'Invalid payment signature') from None
        
class CancelSubscriptionRequest(RequestModel):
    user_id: str
    location_id: str

@app.post("/api/billing/cancel")
async def cancel_subscription(req: CancelSubscriptionRequest):
    profile = await ensure_location_profile(req.location_id, req.user_id)
    (await run_in_threadpool(lambda: supabase.table('location_profiles').update({'auto_renew': False}).eq('location_id', req.location_id).eq('user_id', req.user_id).execute()))
    return {'status': 'success', 'message': 'This is a prepaid plan with no automatic renewal. Access continues until expiry.', 'subscription_end': profile.get('subscription_end')}

@app.post("/api/payment/create-topup-order")
async def create_topup_order(req: TopUpRequest):
    await ensure_user_profile(req.user_id)
    if req.pack_id not in TOP_UP_PACKS:
        raise HTTPException(400, 'Invalid top-up pack')
    return (await run_in_threadpool(lambda: billing.create_order(supabase, razorpay_client, req.user_id, req.location_id, 'topup', req.pack_id, TOP_UP_PACKS[req.pack_id], req.promo_code)))

@app.post("/api/payment/verify-topup")
async def verify_topup(req: VerifyRequest):
    try:
        return (await run_in_threadpool(lambda: billing.verify_order(supabase, razorpay_client, req, 'topup')))
    except razorpay.errors.SignatureVerificationError:
        raise HTTPException(400, 'Invalid payment signature') from None

class ValidatePromoRequest(RequestModel):
    code: str
    user_id: str

@app.post("/api/payment/validate-promo")
async def validate_promo(req: ValidatePromoRequest):
    valid = (await run_in_threadpool(lambda: billing.promo_valid(req.code)))
    return {'valid': valid, 'discount_percent': 100 if valid else 0, 'description': 'One-time promo' if valid else 'Invalid promo code'}

@app.get("/api/payment/key")
async def get_razorpay_key():
    return {"key": os.getenv("RAZORPAY_KEY_ID", "")}

@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "service": "gbp-auto-master-backend"}

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
        limit = PRICING_PLANS[profile['plan_type']]['max_keywords']
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
    limit = PRICING_PLANS[profile['plan_type']]['max_keywords']
    keywords = req.settings.get('active_keywords', [])
    if not isinstance(keywords, list) or any(not isinstance(k,str) or len(k)>120 for k in keywords):
        raise HTTPException(422, 'Keywords must be a list of short text values')
    if len(keywords)>limit:
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
                refresh_token = meta.get('google_refresh_token')
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
    return await run_review_job()


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
                                  'tokens': await get_token_balance(None, req.user_id), 'subscribed': profile['plan_type'] != 'free',
                                  'plan_details': {'plan_id': profile['plan_type'], 'expires_at': profile.get('subscription_end')}})
            page = data.get('nextPageToken')
            if not page: break
    (await run_in_threadpool(lambda: supabase.auth.admin.update_user_by_id(req.user_id, {'user_metadata': {'cached_locations': locations}})))
    return {'status': 'success', 'locations': locations}

class GoogleReviewRequest(RequestModel):
    user_id: str
    provider_token: str
    location_id: str

@app.post("/api/google/get-reviews")
async def get_google_reviews(req: GoogleReviewRequest):
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        # 1. Fetch account to construct full v4 path
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
            # If Google API fails (e.g. they don't have access or billing is disabled for reviews API)
            return {"status": "error", "message": resp.text}
            
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
            "totalFetched": total_fetched
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
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        acc_url = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
        acc_resp = (await run_in_threadpool(lambda: requests.get(acc_url, headers=headers)))
        if not acc_resp.ok:
            return {"status": "error", "message": f"Google Account Fetch Error: {acc_resp.text}"}
            
        accounts = acc_resp.json().get('accounts', [])
        if not accounts:
            return {"status": "error", "message": "No Google Business Accounts found."}
            
        account_name = (await run_in_threadpool(lambda: registered_account(req.location_id)))
        
        # Tell Google to send notifications to our topic
        notif_url = f"https://mybusinessnotifications.googleapis.com/v1/{account_name}/notificationSetting"
        payload = {
            "pubsubTopic": "projects/steady-ether-500708-n8/topics/gbp-reviews-topic",
            "notificationTypes": ["NEW_REVIEW", "UPDATED_REVIEW"]
        }
        
        # We need to specify updateMask for PATCH requests in Google APIs
        resp = (await run_in_threadpool(lambda: requests.patch(notif_url, headers=headers, json=payload, params={"updateMask": "pubsubTopic,notificationTypes"})))
        
        if resp.ok:
            return {"status": "success", "message": "Webhook successfully registered with Google!"}
        else:
            return {"status": "error", "message": f"Google API Error: {resp.text}"}
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
            return {"status": "error", "message": resp.text}
            
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
            return {"status": "error", "message": f"Google refused reply: {reply_resp.text}"}
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
            return {"status": "error", "message": f"Google refused to delete reply: {reply_resp.text}"}
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

async def publish_local_post(req: PublishPostRequest):
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
            return {"status": "error", "message": f"Analytics Fetch Error (Tried {url}): {resp.text}"}
            
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
            return {"status": "error", "message": resp.text}
            
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
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        
        # 1. Fetch published posts older than today that still have images
        posts = (await run_in_threadpool(lambda: supabase.table('calendar_posts')\
            .select('*')\
            .eq('status', 'published')\
            .lt('post_date', yesterday)\
            .not_is('image_url', 'null')\
            .execute()))
            
        deleted_count = 0
        
        for p in posts.data:
            # image_url format: https://xyz.supabase.co/storage/v1/object/public/calendar_images/USER_ID/FILENAME.jpg
            # Extract just the "USER_ID/FILENAME.jpg" part
            if 'calendar_images/' in p['image_url']:
                file_path = p['image_url'].split('calendar_images/')[1]
                
                # Delete from storage
                res = supabase.storage.from_('calendar_images').remove([file_path])
                
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
        raise Exception(f"OAuth error: {resp.text}")
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
            return {"status": "error", "message": "Supabase not configured"}
            
        user_data = (await run_in_threadpool(lambda: supabase.auth.admin.get_user_by_id(req.user_id)))
        if not user_data.user:
            return {"status": "error", "message": "User not found"}
            
        refresh_token = user_data.user.user_metadata.get('google_refresh_token')
        if not refresh_token:
            return {"status": "error", "message": "No refresh token found for this user"}
            
        new_access_token = (await run_in_threadpool(lambda: get_offline_access_token(refresh_token)))
        return {"status": "success", "provider_token": new_access_token}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/cron/publish-scheduled")
async def publish_scheduled_posts():
    due = datetime.now(timezone.utc).isoformat()
    posts = (await run_in_threadpool(lambda: supabase.table('calendar_posts').select('*').eq('status','scheduled').lte('publish_at',due).execute())).data
    published = 0
    failed = 0
    for post in posts:
        try:
            user = (await run_in_threadpool(lambda: supabase.auth.admin.get_user_by_id(post['user_id']))).user
            refresh = (user.user_metadata or {}).get('google_refresh_token')
            if not refresh:
                failed += 1
                continue
            token = (await run_in_threadpool(lambda: get_offline_access_token(refresh)))
            await publish_calendar_post(post['id'], post['user_id'], post['location_id'], token)
            published += 1
        except Exception:
            failed += 1
    if failed:
        raise HTTPException(502, f'{published} posts published; {failed} need attention. Inspect calendar post status and logs.')
    return {'status':'success','published':published}

@app.get("/api/cron/daily-backlog-reviews")
async def daily_backlog_reviews():
    return await run_review_job()

import base64
import json

@app.post("/api/webhooks/google-reviews")
async def google_reviews_webhook(req: Request):
    import re
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
    profile = await ensure_location_profile(location, user_id)
    if profile['plan_type'] == 'free': return {'status': 'ignored', 'reason': 'No active plan'}
    user = (await run_in_threadpool(lambda: supabase.auth.admin.get_user_by_id(user_id))).user
    refresh_token = (user.user_metadata or {}).get('google_refresh_token')
    if not refresh_token: return {'status': 'ignored', 'reason': 'Reconnect Google'}
    token = (await run_in_threadpool(lambda: get_offline_access_token(refresh_token)))
    response = (await run_in_threadpool(lambda: requests.get(f'https://mybusiness.googleapis.com/v4/{review_name}', headers={'Authorization': f'Bearer {token}'})))
    if not response.ok: raise HTTPException(502, 'Google review lookup failed')
    review = response.json()
    if review.get('reviewReply'): return {'status': 'ignored', 'reason': 'Already replied'}
    settings = await get_ai_settings(GetAISettingsRequest(user_id=user_id, location_id=location))
    if not settings.get('is_ai_active', True) or (review.get('starRating') in ('ONE','TWO') and not settings.get('reply_to_1_star', False)):
        return {'status': 'ignored', 'reason': 'Disabled in settings'}
    try:
        return await generate_and_publish(user_id, location, token, review, settings)
    except HTTPException as error:
        if error.status_code in (402,409): return {'status': 'ignored', 'reason': error.detail}
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
        profile = {**profile, **location, 'max_seo_keywords': PRICING_PLANS[location['plan_type']]['max_keywords']}
    return profile


@app.post('/api/user/reset-onboarding')
async def reset_onboarding(req: UserProfileRequest):
    (await run_in_threadpool(lambda: supabase.table('user_profiles').update({'onboarding_completed': False}).eq('id',req.user_id).execute()))
    return {'status': 'success'}

@app.post("/api/user/onboarding")
async def save_onboarding(req: OnboardingRequest):
    profile = await ensure_user_profile(req.user_id)
    
    # Validate keyword count (free plan = max 2)
    plan = PRICING_PLANS.get(profile.get('plan_type', 'free'), PRICING_PLANS['free'])
    max_kw = plan['max_keywords']
    keywords = req.seo_keywords[:max_kw]  # Trim to allowed count
    
    update_data = {
        'reply_length': req.reply_length,
        'seo_keywords': keywords,
        'onboarding_completed': True,
        'updated_at': datetime.utcnow().isoformat()
    }
    if req.full_name:
        update_data['full_name'] = req.full_name
    
    (await run_in_threadpool(lambda: supabase.table('user_profiles').update(update_data).eq('id', req.user_id).execute()))
    
    return {'status': 'success', 'keywords_saved': len(keywords), 'max_allowed': max_kw}

# ─── Token System ───

@app.post("/api/tokens/claim-daily")
async def claim_daily_tokens(req: DailyClaimRequest):
    return {'status': 'error', 'message': 'Daily tokens feature has been deprecated in favor of the new 200 token sign-up bonus.'}

@app.post("/api/tokens/balance")
async def get_token_balance_endpoint(req: TokenBalanceRequest):
    account = await ensure_user_profile(req.user_id)
    locations = (await run_in_threadpool(lambda: supabase.table('location_profiles').select('location_id').eq('user_id', req.user_id).execute())).data
    for row in locations:
        (await run_in_threadpool(lambda: supabase.rpc('refresh_monthly_tokens', {'p_location': row['location_id'], 'p_user': req.user_id}).execute()))
    account = await ensure_user_profile(req.user_id)
    profile = await ensure_location_profile(req.location_id, req.user_id) if req.location_id else None
    ledger = (await run_in_threadpool(lambda: supabase.table('account_token_ledger').select('*').eq('user_id', req.user_id).order('created_at', desc=True).limit(50).execute()))
    return {'balance': float(account.get('tokens_balance') or 0), 'scope': 'account',
            'plan_type': profile['plan_type'] if profile else 'free', 'history': ledger.data or []}

@app.post("/api/tokens/redeem-promo")
async def redeem_promo_code(req: PromoCodeRequest):
    await ensure_user_profile(req.user_id)
    if not (await run_in_threadpool(lambda: billing.promo_valid(req.promo_code))):
        raise HTTPException(400, 'Invalid promo code')
    return (await run_in_threadpool(lambda: billing.create_order(supabase, razorpay_client, req.user_id, req.location_id, 'promo', 'token_promo', {'price': 0, 'tokens': 1000}, req.promo_code)))

# ─── Batch Review Reply with Token System ───

@app.post("/api/reviews/batch-reply")
async def batch_reply_reviews(req: BatchReplyRequest):
    profile = await ensure_location_profile(req.location_id, req.user_id)
    headers = {'Authorization': f'Bearer {req.access_token}'}
    response = (await run_in_threadpool(lambda: requests.get(f"https://mybusiness.googleapis.com/v4/{profile['account_id']}/{req.location_id}/reviews", headers=headers)))
    if not response.ok:
        raise HTTPException(502, 'Could not fetch Google reviews')
    settings = await get_ai_settings(GetAISettingsRequest(user_id=req.user_id, location_id=req.location_id))
    if not settings.get('is_ai_active', True):
        return {'status': 'success', 'replied': [], 'tokens_used': 0, 'message': 'AI is disabled for this location'}
    reviews = [r for r in response.json().get('reviews', []) if not r.get('reviewReply')]
    if not settings.get('reply_to_1_star', False):
        reviews = [r for r in reviews if r.get('starRating') not in ('ONE','TWO')]
    replies = []
    for review in reviews[:req.count]:
        try:
            result = await generate_and_publish(req.user_id, req.location_id, req.access_token, review, settings)
            replies.append({'review_name': review['name'], **result})
        except HTTPException as error:
            replies.append({'review_name': review['name'], 'status': 'failed', 'error': error.detail})
            if error.status_code == 402: break
    return {'status': 'success', 'replied': replies, 'tokens_used': sum(2.5 for r in replies if r['status']=='published'),
            'balance': await get_token_balance(req.location_id, req.user_id)}

@app.post("/api/reviews/regenerate-reply")
async def regenerate_reply(req: RegenerateReplyRequest):
    settings = await get_ai_settings(GetAISettingsRequest(user_id=req.user_id, location_id=req.location_id))
    review = {'name': req.review_name, 'comment': req.review_text, 'starRating': req.star_rating}
    return await generate_and_publish(req.user_id, req.location_id, req.access_token, review, settings, regenerate=True)

# ─── Rank Report with Token System ───

@app.post("/api/rank/generate-report")
async def generate_rank_report(req: RankReportRequest):
    profile = await ensure_location_profile(req.location_id, req.user_id)
    if not PRICING_PLANS[profile['plan_type']]['competitor']:
        raise HTTPException(403, 'Competitor reports require a Growth or Yearly plan')
    maps_key = os.getenv('GOOGLE_MAPS_API_KEY')
    if not maps_key: raise HTTPException(503, 'Competitor lookup is not configured')
    operation = 'report:' + uuid4().hex
    (await run_in_threadpool(lambda: reserve_operation(req.location_id, req.user_id, 10, operation)))
    try:
        response = (await run_in_threadpool(lambda: requests.get(f'https://mybusinessbusinessinformation.googleapis.com/v1/{req.location_id}',
            headers={'Authorization': f'Bearer {req.access_token}'}, params={'readMask': 'title,storefrontAddress'})))
        if not response.ok: raise HTTPException(502, 'Could not load your business details')
        business = response.json()
        address = ', '.join(str(v) for k,v in business.get('storefrontAddress', {}).items() if k in ('locality','administrativeArea','regionCode'))
        if not address: raise HTTPException(422, 'A business address is required for local competitor lookup')
        response = (await run_in_threadpool(lambda: requests.get('https://maps.googleapis.com/maps/api/place/textsearch/json', params={'query': f'{req.keyword} near {address}', 'key': maps_key})))
        if not response.ok or response.json().get('status') != 'OK':
            raise HTTPException(502, 'Competitor data is unavailable; no tokens charged')
        competitors = [{'name': p['name'], 'rating': p.get('rating'), 'reviews': p.get('user_ratings_total')} for p in response.json().get('results', []) if p.get('name') != business.get('title')][:10]
        if not competitors: raise HTTPException(404, 'No competitors found; no tokens charged')
        prompt = f"Analyze these Google Places results for {business.get('title')} in {address}, keyword {req.keyword}: {competitors}. Distinguish measured ratings and review counts from hypotheses. This is not a measured search-rank report. Do not invent facts. Provide a concise action plan."
        report = (await run_in_threadpool(lambda: call_groq_with_fallback(os.getenv('GROQ_API_KEY'), [{'role':'user','content':prompt}]))).choices[0].message.content
        if not report: raise HTTPException(502, 'AI returned an empty report')
    except Exception:
        (await run_in_threadpool(lambda: finish_operation(operation, False)))
        raise
    (await run_in_threadpool(lambda: finish_operation(operation, True)))
    return {'status':'success','keyword':req.keyword,'business':business.get('title'),'location':address,
            'competitors':competitors,'report':report,'balance':await get_token_balance(req.location_id,req.user_id)}




async def location_subscriptions(user_id: str) -> dict:
    rows = (await run_in_threadpool(lambda: supabase.table('location_profiles').select('*').eq('user_id', user_id).execute())).data
    result = {}
    for row in rows:
        profile = await ensure_location_profile(row['location_id'], user_id)
        if profile['plan_type'] != 'free':
            result[row['location_id']] = {'status': 'active', 'plan_id': profile['plan_type'], 'expires_at': profile['subscription_end']}
    return result


def reserve_operation(location_id, user_id, amount, operation_id):
    result = supabase.rpc('reserve_tokens', {'p_operation': operation_id, 'p_location': normalize_location(location_id) if location_id else None, 'p_user': user_id, 'p_amount': amount}).execute().data
    if not result['success']:
        raise HTTPException(409 if 'already' in result.get('error', '') else 402, result.get('error', 'Insufficient tokens'))
    return result


def finish_operation(operation_id, success):
    supabase.rpc('finish_tokens', {'p_operation': operation_id, 'p_success': success}).execute()


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
    (await run_in_threadpool(lambda: reserve_operation(location_id, user_id, 2.5, operation)))
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
    if not response.ok: raise HTTPException(502, 'Google rejected the reply; tokens refunded')
    return {'status':'published','ai_reply':reply,'balance':await get_token_balance(location_id,user_id)}


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


@app.post('/api/google/publish-post')
async def publish_immediate_post(req: PublishPostRequest, request: Request):
    owner = (await run_in_threadpool(lambda: supabase.table('location_profiles').select('user_id').eq('location_id',req.location_id).single().execute())).data['user_id']
    operation = 'publish:' + uuid4().hex
    (await run_in_threadpool(lambda: reserve_operation(req.location_id,owner,5,operation)))
    result = await publish_local_post(req)
    (await run_in_threadpool(lambda: finish_operation(operation,result.get('status')=='success')))
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
        refresh = (user.user_metadata or {}).get('google_refresh_token')
        if not subscriptions or not refresh:
            continue
        try:
            token = (await run_in_threadpool(get_offline_access_token, refresh))
        except Exception:
            failed += 1
            continue
        for location in subscriptions:
            try:
                result = await sync_and_reply_reviews(GoogleReviewRequest(user_id=user.id, location_id=location, provider_token=token))
                sent += sum(1 for reply in result.get('replied',[]) if reply['status']=='published')
                failed += len(result.get('failures',[]))
            except Exception:
                failed += 1
    if failed:
        raise HTTPException(502, f'{sent} replies sent; {failed} operations need attention. Inspect token_operations and Google connections.')
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
