import os
import razorpay
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="GBP Auto Master Backend")

# Allow frontend to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Update this to specific frontend URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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

PRICING_PLANS = {
    'half_yearly': {'original': 2999, 'discounted': 1999},
    'yearly': {'original': 5500, 'discounted': 3999}
}

class OrderRequest(BaseModel):
    plan_id: str
    promo_code: str
    user_id: str # Supabase User ID
    location_id: str = None

@app.post("/api/payment/create-order")
async def create_order(req: OrderRequest):
    if req.plan_id not in PRICING_PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan selected")
        
    base_price = PRICING_PLANS[req.plan_id]['original']
    final_price = PRICING_PLANS[req.plan_id]['discounted'] # Default to discounted for first-time users
    
    if req.promo_code == 'ATYAUNSUHJ':
        final_price = 0
        
    if final_price == 0:
        # 100% discount, bypass Razorpay, activate directly
        if not supabase:
            raise HTTPException(status_code=500, detail="Supabase not configured")
        try:
            # Bypass logic
            if req.promo_code == 'ATYAUNSUHJ':
                user_data = supabase.auth.admin.get_user_by_id(req.user_id)
                user_meta = user_data.user.user_metadata if user_data.user else {}
                
                subs = user_meta.get("subscriptions", {})
                import datetime as dt
                now = dt.datetime.now()
                months = 1 if req.plan_id == 'monthly' else (6 if req.plan_id == 'half_yearly' else 12)
                expires_at = (now + dt.timedelta(days=30*months)).isoformat()
                
                if req.location_id:
                    subs[req.location_id] = {
                        "plan_id": req.plan_id,
                        "expires_at": expires_at,
                        "status": "active"
                    }
                    
                    supabase.auth.admin.update_user_by_id(
                        req.user_id,
                        {"user_metadata": {"subscriptions": subs}}
                    )
                return {"status": "success", "message": "Location subscription activated", "free_trial": True}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
    # Generate Razorpay Order
    data = {
        "amount": int(final_price * 100), # Subunits (ensure int)
        "currency": "INR",
        "receipt": f"receipt_{req.user_id[:8]}",
        "notes": {
            "plan_id": req.plan_id,
            "user_id": req.user_id,
            "location_id": str(req.location_id) if req.location_id else "unknown"
        }
    }
    
    try:
        rzp = razorpay.Client(auth=(os.getenv('RAZORPAY_KEY_ID', ''), os.getenv('RAZORPAY_KEY_SECRET', '')))
        order = rzp.order.create(data=data)
        return {"status": "success", "order_id": order['id'], "amount": data['amount']}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class VerifyRequest(BaseModel):
    razorpay_payment_id: str
    razorpay_order_id: str
    razorpay_signature: str
    user_id: str
    location_id: str = None
    plan_id: str = None

@app.post("/api/payment/verify")
async def verify_payment(req: VerifyRequest):
    try:
        # Cryptographically verify the signature
        params_dict = {
            'razorpay_order_id': req.razorpay_order_id,
            'razorpay_payment_id': req.razorpay_payment_id,
            'razorpay_signature': req.razorpay_signature
        }
        
        rzp = razorpay.Client(auth=(os.getenv('RAZORPAY_KEY_ID', ''), os.getenv('RAZORPAY_KEY_SECRET', '')))
        rzp.utility.verify_payment_signature(params_dict)
        
        # If we get here, signature is valid! Securely update Supabase.
        if not supabase:
            raise HTTPException(status_code=500, detail="Supabase not configured")
            
        # Fetch current metadata to append location
        user_data = supabase.auth.admin.get_user_by_id(req.user_id)
        user_meta = user_data.user.user_metadata if user_data.user else {}
        subs = user_meta.get("subscriptions", {})
        
        if req.location_id and req.plan_id:
            import datetime as dt
            now = dt.datetime.now()
            months = 1 if req.plan_id == 'monthly' else (6 if req.plan_id == 'half' else 12)
            expires_at = (now + dt.timedelta(days=30*months)).isoformat()
            
            subs[req.location_id] = {
                "plan_id": req.plan_id,
                "expires_at": expires_at,
                "status": "active"
            }
            
            supabase.auth.admin.update_user_by_id(req.user_id, {"user_metadata": {"subscriptions": subs}})
        
        return {"status": "success", "message": "Payment verified and subscription activated"}
    except razorpay.errors.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid payment signature")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
class CancelSubscriptionRequest(BaseModel):
    user_id: str
    location_id: str

@app.post("/api/billing/cancel")
async def cancel_subscription(req: CancelSubscriptionRequest):
    try:
        if not supabase:
            raise HTTPException(status_code=500, detail="Supabase not configured")
            
        user_data = supabase.auth.admin.get_user_by_id(req.user_id)
        if not user_data.user:
            raise HTTPException(status_code=404, detail="User not found")
            
        user_meta = user_data.user.user_metadata or {}
        subs = user_meta.get("subscriptions", {})
        
        if req.location_id in subs:
            subs[req.location_id]['auto_renew'] = False
            supabase.auth.admin.update_user_by_id(
                req.user_id,
                {"user_metadata": {"subscriptions": subs}}
            )
            return {"status": "success", "message": "Subscription cancelled. It will remain active until the end of the current billing cycle."}
        else:
            raise HTTPException(status_code=404, detail="Subscription not found for this location")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/payment/key")
async def get_razorpay_key():
    return {"key": os.getenv("RAZORPAY_KEY_ID", "")}

@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "service": "gbp-auto-master-backend"}

class SaveAISettingsRequest(BaseModel):
    user_id: str
    location_id: str
    settings: dict

@app.post("/api/user/save-ai-settings")
async def save_ai_settings(req: SaveAISettingsRequest):
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    try:
        user_data = supabase.auth.admin.get_user_by_id(req.user_id)
        if not user_data.user:
            raise HTTPException(status_code=404, detail="User not found")
            
        user_meta = user_data.user.user_metadata or {}
        ai_settings = user_meta.get("ai_settings", {})
        
        ai_settings[req.location_id] = req.settings
        
        supabase.auth.admin.update_user_by_id(
            req.user_id,
            {"user_metadata": {"ai_settings": ai_settings}}
        )
        
        # Also sync to the user_settings table
        try:
            supabase.table('user_settings').upsert({
                'user_id': req.user_id,
                'location_id': req.location_id,
                'active_keywords': req.settings.get('active_keywords', []),
                'is_ai_active': req.settings.get('is_ai_active', True)
            }).execute()
        except Exception as table_err:
            print("Failed to sync to user_settings table:", table_err)
            
        return {"status": "success", "message": "AI settings saved successfully"}
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        return {"status": "error", "message": f"Backend Crash: {str(e)} | Trace: {error_details}"}

class AdminAuthRequest(BaseModel):
    admin_email: str

@app.post("/api/admin/users")
async def get_all_users(req: AdminAuthRequest):
    if req.admin_email not in ['ayushsony126@gmail.com', 'aryansoni12567@gmail.com']:
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
        
    try:
        users = supabase.auth.admin.list_users()
        user_list = []
        for u in users:
            meta = u.user_metadata or {}
            user_list.append({
                "id": u.id,
                "email": u.email,
                "created_at": str(u.created_at),
                "full_name": meta.get("full_name"),
                "demo_used": meta.get("demo_used", False),
                "subscriptions": meta.get("subscriptions", {}),
                "has_google_token": bool(meta.get("google_refresh_token"))
            })
        return {"status": "success", "users": user_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class AdminUserRequest(BaseModel):
    admin_email: str
    target_user_id: str

@app.post("/api/admin/calendar")
async def admin_get_calendar(req: AdminUserRequest):
    if req.admin_email not in ['ayushsony126@gmail.com', 'aryansoni12567@gmail.com']:
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
        
    try:
        posts = supabase.table('calendar_posts').select('*').eq('user_id', req.target_user_id).order('post_date', desc=True).execute()
        return {"status": "success", "posts": posts.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/run-competitor-scan")
async def run_competitor_scan(req: AdminAuthRequest):
    if req.admin_email != 'ayushsony126@gmail.com':
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
        
    import datetime as dt
    from groq import Groq
    try:
        users = supabase.auth.admin.list_users()
        scanned_count = 0
        
        for u in users:
            meta = u.user_metadata or {}
            subs = meta.get('subscriptions', {})
            intel = meta.get('competitor_intel', {})
            
            # For this MVP demo, let's generate intel for loc1 or any active subscription
            loc_ids = list(subs.keys()) if subs else ['loc1']
            
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
                import requests
                if refresh_token:
                    try:
                        access_token = get_offline_access_token(refresh_token)
                        clean_loc_id = loc_id if loc_id.startswith('locations/') else f"locations/{loc_id}"
                        headers = {"Authorization": f"Bearer {access_token}"}
                        loc_url = f"https://mybusinessbusinessinformation.googleapis.com/v1/{clean_loc_id}?readMask=name,title,storefrontAddress"
                        loc_resp = requests.get(loc_url, headers=headers)
                        if loc_resp.ok:
                            loc_data = loc_resp.json()
                            real_business_name = loc_data.get('title')
                            address = loc_data.get('storefrontAddress', {})
                            business_city = address.get('locality')
                            business_country = address.get('regionCode')
                            
                        acc_url = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
                        acc_resp = requests.get(acc_url, headers=headers)
                        if acc_resp.ok:
                            accounts = acc_resp.json().get('accounts', [])
                            if accounts:
                                account_name = accounts[0]['name']
                                rev_url = f"https://mybusiness.googleapis.com/v4/{account_name}/{clean_loc_id}/reviews"
                                rev_resp = requests.get(rev_url, headers=headers)
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
                    res = requests.get("https://serpapi.com/search", params=params)
                    data = res.json()
                    
                    if "error" in data:
                        error_msg = data["error"]
                        print("SERPAPI ERROR:", error_msg)
                        local_results = [
                            {"title": f"⚠️ SerpApi Error: {error_msg}", "rating": 0.0, "reviews": 0},
                            {"title": "Please check your SerpApi key and billing.", "rating": 0.0, "reviews": 0}
                        ]
                    elif not data.get("local_results"):
                        # Fallback if SerpApi returns empty (no map pack)
                        print(f"SERPAPI WARNING: No local_results found for query '{search_query}'.")
                        local_results = [
                            {"title": f"⚠️ No Local Map Pack found for '{search_query}'", "rating": 0.0, "reviews": 0},
                            {"title": "Try adding a more specific SEO keyword like 'Plumber in New York'.", "rating": 0.0, "reviews": 0},
                            {"title": real_business_name or meta.get('full_name') or 'Your Business', "rating": 5.0, "reviews": 1}
                        ]
                    else:
                        local_results = data.get("local_results")
                        
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
                            "rating": float(place.get('rating', 4.0)),
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
                        chat_completion = call_groq_with_fallback(groq_api_key, [{"role": "user", "content": prompt}])
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
            supabase.auth.admin.update_user_by_id(u.id, {"user_metadata": {"competitor_intel": intel}})
            
        return {"status": "success", "message": f"Successfully ran competitor scan and generated AI Reports for {scanned_count} locations."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/cron/reply-reviews")
async def cron_reply_reviews():
    """
    Background worker triggered by cron-job.org.
    It loops through all users and locations, calling the sync logic.
    """
    try:
        if not supabase:
            return {"status": "error", "message": "Supabase not configured"}
            
        users = supabase.auth.admin.list_users()
        total_replies = 0
        
        for u in users:
            meta = u.user_metadata or {}
            subs = meta.get('subscriptions', {})
            refresh_token = meta.get('google_refresh_token')
            
            if not refresh_token:
                continue
                
            try:
                access_token = get_offline_access_token(refresh_token)
            except Exception:
                continue # Skip if token refresh fails
                
            for loc_id, sub_data in subs.items():
                if sub_data.get('status') == 'active':
                    # Extract the location ID part if it contains the full path
                    clean_loc = loc_id.split('/')[-1] if '/' in loc_id else loc_id
                    
                    try:
                        req = GoogleReviewRequest(
                            user_id=u.id,
                            provider_token=access_token,
                            location_id=clean_loc
                        )
                        res = await sync_and_reply_reviews(req)
                        if res.get('status') == 'success':
                            # Assuming "AI sent X replies" is in the message
                            import re
                            match = re.search(r'sent (\d+) replies', res.get('message', ''))
                            if match:
                                total_replies += int(match.group(1))
                    except Exception:
                        pass # Continue to next location even if one fails
                        
        return {"status": "success", "message": f"Cron Job finished. Total automated replies sent: {total_replies}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==========================================
# GOOGLE BUSINESS PROFILE & AI ENGINE
# ==========================================

import requests
from groq import Groq
import itertools

# We use the user-provided Groq key securely from Environment Variables
def call_groq_with_fallback(api_key: str, messages: list, temperature: float = 0.2):
    client = Groq(api_key=api_key or os.getenv('GROQ_API_KEY'))
    models = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768",
        "gemma2-9b-it"
    ]
    last_e = None
    for m in models:
        try:
            return client.chat.completions.create(messages=messages, model=m, temperature=temperature)
        except Exception as e:
            last_e = e
    raise last_e

def generate_ai_reply(prompt: str) -> str:
    """
    Generates a reply using Groq's Llama 3 model (100% free and lightning fast).
    """
    chat_completion = call_groq_with_fallback(os.getenv('GROQ_API_KEY'), [
        {
            "role": "system",
            "content": "You are a professional customer service AI. Write extremely short (max 2 sentences) and polite replies to customer reviews. NEVER repeat the exact same phrasing. Vary your vocabulary and tone slightly so every reply is unique and personalized."
        },
        {
            "role": "user",
            "content": prompt,
        }
    ], temperature=0.75)
    return chat_completion.choices[0].message.content

class GoogleSyncRequest(BaseModel):
    user_id: str
    provider_token: str = None

from typing import List

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatContextRequest(BaseModel):
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
        
        chat_completion = call_groq_with_fallback(os.getenv('GROQ_API_KEY'), messages)
        
        return {"status": "success", "reply": chat_completion.choices[0].message.content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class ReportContextRequest(BaseModel):
    user_id: str
    context_dump: str

@app.post("/api/ai/generate-report")
async def generate_report(req: ReportContextRequest):
    try:
        prompt = f"Analyze this Google Business Profile context: {req.context_dump}. Write a 3-sentence executive summary and 3 bullet-point action items for the business owner to improve their ranking and engagement. Format exactly as:\nSUMMARY: [text]\nACTION 1: [text]\nACTION 2: [text]\nACTION 3: [text]"
        chat_completion = call_groq_with_fallback(os.getenv('GROQ_API_KEY'), [{"role": "user", "content": prompt}])
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/google/locations")
async def get_google_locations(req: GoogleSyncRequest):
    """
    Fetches the user's provider token from the frontend request,
    and calls the Google Business Profile API to list their locations.
    """
    try:
        if not req.provider_token:
            return {"status": "error", "message": "Missing Google provider token. Please log out and log in again."}
            
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        # 1. Fetch Accounts
        acc_url = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
        acc_resp = requests.get(acc_url, headers=headers)
        
        if not acc_resp.ok:
            return {"status": "error", "message": f"Google API Error (Accounts): {acc_resp.text}"}
            
        accounts = acc_resp.json().get('accounts', [])
        if not accounts:
            return {"status": "success", "locations": [], "message": "No Google Business Accounts found on this email."}
            
        # 2. Fetch Locations for the first account
        account_name = accounts[0]['name']
        loc_url = f"https://mybusinessbusinessinformation.googleapis.com/v1/{account_name}/locations?readMask=name,title"
        
        loc_resp = requests.get(loc_url, headers=headers)
        if not loc_resp.ok:
            return {"status": "error", "message": f"Google API Error (Locations): {loc_resp.text}"}
            
        google_locations = loc_resp.json().get('locations', [])
        
        # 3. Check Subscriptions
        user_data = supabase.auth.admin.get_user_by_id(req.user_id)
        user_meta = user_data.user.user_metadata if user_data.user else {}
        subs = user_meta.get("subscriptions", {})
        
        # 4. Format for dashboard
        dashboard_locations = []
        for loc in google_locations:
            loc_id = loc.get("name")
            is_sub = loc_id in subs and subs[loc_id].get("status") == "active"
            dashboard_locations.append({
                "id": loc_id, # e.g. "locations/12345"
                "name": loc.get("title", "Unnamed Location"),
                "reviews": 0, 
                "rating": 0.0,
                "subscribed": is_sub,
                "plan_details": subs.get(loc_id, None)
            })
            
        return {
            "status": "success", 
            "locations": dashboard_locations
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class GoogleReviewRequest(BaseModel):
    user_id: str
    provider_token: str
    location_id: str

@app.post("/api/google/get-reviews")
async def get_google_reviews(req: GoogleReviewRequest):
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        # 1. Fetch account to construct full v4 path
        acc_url = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
        acc_resp = requests.get(acc_url, headers=headers)
        if not acc_resp.ok:
            return {"status": "error", "message": f"Google Account Fetch Error: {acc_resp.text}"}
        accounts = acc_resp.json().get('accounts', [])
        if not accounts:
            return {"status": "error", "message": "No Google Business Accounts found."}
        account_name = accounts[0]['name']
        full_location_path = f"{account_name}/{req.location_id}"
        
        url = f"https://mybusiness.googleapis.com/v4/{full_location_path}/reviews"
        resp = requests.get(url, headers=headers)
        
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/google/run-demo")
async def run_google_demo(req: GoogleReviewRequest):
    """
    Fetches the 2 newest unreplied reviews, uses AI to generate a reply, 
    AND actually posts the replies live to Google as a magic-moment demo.
    """
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        # 1. Fetch account
        acc_url = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
        acc_resp = requests.get(acc_url, headers=headers)
        if not acc_resp.ok:
            return {"status": "error", "message": f"Google Account Fetch Error: {acc_resp.text}"}
        accounts = acc_resp.json().get('accounts', [])
        if not accounts:
            return {"status": "error", "message": "No Google Business Accounts found."}
        account_name = accounts[0]['name']
        full_location_path = f"{account_name}/{req.location_id}"
        
        # 2. Fetch Reviews
        url = f"https://mybusiness.googleapis.com/v4/{full_location_path}/reviews"
        resp = requests.get(url, headers=headers)
        
        if not resp.ok:
            return {"status": "error", "message": resp.text}
            
        data = resp.json().get("reviews", [])
        
        # 3. Find up to 2 unreplied reviews
        unreplied = [r for r in data if "reviewReply" not in r]
        to_reply = unreplied[:2]
        
        if not to_reply:
            return {"status": "error", "message": "No unanswered reviews found on this profile to run the demo!"}
            
        replies_generated = []
        
        # 4. Generate and Post Replies
        for rev in to_reply:
            reviewer_name = rev.get('reviewer', {}).get('displayName', 'Valued Customer')
            comment = rev.get('comment', 'No text provided.')
            star_rating = rev.get('starRating', 'FIVE')
            
            prompt = f"Customer Name: {reviewer_name}\nRating: {star_rating}\nReview: {comment}\n\nWrite a friendly, SEO-optimized reply from the business owner."
            
            ai_reply = generate_ai_reply(groq_api_key, prompt)
            
            # Post back to Google
            reply_url = f"https://mybusiness.googleapis.com/v4/{rev['name']}/reply"
            reply_resp = requests.put(reply_url, headers=headers, json={"comment": ai_reply})
            
            if reply_resp.ok:
                replies_generated.append({
                    "reviewer": reviewer_name,
                    "comment": comment,
                    "ai_reply": ai_reply
                })
                
        return {"status": "success", "replies": replies_generated}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/google/sync-reviews")
async def sync_and_reply_reviews(req: GoogleReviewRequest):
    """
    Fetches unreplied reviews, uses Gemini to generate SEO-optimized replies, 
    and posts them back to Google.
    """
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        acc_url = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
        acc_resp = requests.get(acc_url, headers=headers)
        if not acc_resp.ok:
            return {"status": "error", "message": f"Google Account Fetch Error: {acc_resp.text}"}
        accounts = acc_resp.json().get('accounts', [])
        if not accounts:
            return {"status": "error", "message": "No Google Business Accounts found."}
        account_name = accounts[0]['name']
        full_location_path = f"{account_name}/{req.location_id}"
        
        url = f"https://mybusiness.googleapis.com/v4/{full_location_path}/reviews"
        resp = requests.get(url, headers=headers)
        
        if not resp.ok:
            return {"status": "error", "message": resp.text}
            
        reviews = resp.json().get('reviews', [])
        
        # Find unreplied reviews and STRICTLY limit to max 4 at a time
        unreplied = [r for r in reviews if 'reviewReply' not in r]
        unreplied = unreplied[:4]
        
        # Fetch user_settings from Supabase
        target_keywords = []
        ai_settings = {}
        if supabase:
            try:
                user_settings = supabase.table('user_settings').select('*').eq('user_id', req.user_id).execute()
                if user_settings.data:
                    ai_settings = user_settings.data[0]
                    target_keywords = ai_settings.get('active_keywords', [])
            except Exception as e:
                print("Error fetching settings from Supabase:", e)
                
        if ai_settings and not ai_settings.get('is_ai_active', True):
            return {"status": "success", "message": "AI Autopilot is currently turned off in settings."}
                
        keyword_instruction = ""
        if target_keywords:
            keyword_list = ", ".join([f'"{k}"' for k in target_keywords])
            keyword_instruction = f"CRITICAL INSTRUCTION: You are an aggressive local SEO engine. You MUST inject at least one of these exact SEO keywords into your reply: {keyword_list}. Do this no matter how vague the customer's review is. Do NOT output a reply without an SEO keyword."
            
        ai_tone = ai_settings.get('ai_tone', 'Professional') if ai_settings else 'Professional'
        custom_instructions = ai_settings.get('custom_instructions', '') if ai_settings else ''
        custom_instruction_text = f"Additional custom instructions from the business owner: {custom_instructions}" if custom_instructions else ""
        
        replies_sent = 0
        
        for r in unreplied:
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
                
                ai_reply = generate_ai_reply(prompt)
                
                # Post reply back to Google API
                reply_url = f"https://mybusiness.googleapis.com/v4/{r.get('name')}/reply"
                reply_resp = requests.put(reply_url, headers=headers, json={"comment": ai_reply})
                
                if reply_resp.ok:
                    replies_sent += 1
                else:
                    return {"status": "error", "message": f"Google refused reply: {reply_resp.text}"}
            except Exception as inner_e:
                return {"status": "error", "message": f"AI Error on review {r.get('name')}: {str(inner_e)}"}
                
        return {
            "status": "success",
            "message": f"Successfully synced. AI sent {replies_sent} replies."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/google/register-webhook")
async def register_google_webhook(req: GoogleReviewRequest):
    """
    Tells Google Business Profile API to start pushing new reviews for this account
    to our specific Pub/Sub topic.
    """
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        acc_url = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
        acc_resp = requests.get(acc_url, headers=headers)
        if not acc_resp.ok:
            return {"status": "error", "message": f"Google Account Fetch Error: {acc_resp.text}"}
            
        accounts = acc_resp.json().get('accounts', [])
        if not accounts:
            return {"status": "error", "message": "No Google Business Accounts found."}
            
        account_name = accounts[0]['name']
        
        # Tell Google to send notifications to our topic
        notif_url = f"https://mybusinessnotifications.googleapis.com/v1/{account_name}/notificationSetting"
        payload = {
            "pubsubTopic": "projects/steady-ether-500708-n8/topics/gbp-reviews-topic",
            "notificationTypes": ["NEW_REVIEW", "UPDATED_REVIEW"]
        }
        
        # We need to specify updateMask for PATCH requests in Google APIs
        resp = requests.patch(notif_url, headers=headers, json=payload, params={"updateMask": "pubsubTopic,notificationTypes"})
        
        if resp.ok:
            return {"status": "success", "message": "Webhook successfully registered with Google!"}
        else:
            return {"status": "error", "message": f"Google API Error: {resp.text}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/google/draft-reviews")
async def draft_google_reviews(req: GoogleReviewRequest):
    """
    Fetches unreplied reviews and generates AI drafts for them WITHOUT posting them.
    Used for the Admin Approval Queue.
    """
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        acc_url = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
        acc_resp = requests.get(acc_url, headers=headers)
        if not acc_resp.ok:
            return {"status": "error", "message": f"Google Account Fetch Error: {acc_resp.text}"}
        accounts = acc_resp.json().get('accounts', [])
        if not accounts:
            return {"status": "error", "message": "No Google Business Accounts found."}
        account_name = accounts[0]['name']
        full_location_path = f"{account_name}/{req.location_id}"
        
        url = f"https://mybusiness.googleapis.com/v4/{full_location_path}/reviews"
        resp = requests.get(url, headers=headers)
        
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
                user_data = supabase.auth.admin.get_user_by_id(req.user_id)
                if user_data.user:
                    all_settings = (user_data.user.user_metadata or {}).get("ai_settings", {})
                    ai_settings = all_settings.get(req.location_id, {})
                    target_keywords = ai_settings.get('active_keywords', [])
            except Exception as e:
                print("Error fetching settings from Supabase metadata:", e)
                
        keyword_instruction = ""
        if target_keywords:
            keyword_list = ", ".join([f'"{k}"' for k in target_keywords])
            keyword_instruction = f"CRITICAL INSTRUCTION: You are an aggressive local SEO engine. You MUST inject at least one of these exact SEO keywords into your reply: {keyword_list}. Do this no matter how vague the customer's review is. Do NOT output a reply without an SEO keyword."
            
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
                
                ai_reply = generate_ai_reply(prompt)
                
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class PostReplyRequest(BaseModel):
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
        reply_resp = requests.put(reply_url, headers=headers, json={"comment": req.reply_text})
        
        if reply_resp.ok:
            return {"status": "success", "message": "Reply posted successfully!"}
        else:
            return {"status": "error", "message": f"Google refused reply: {reply_resp.text}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class DeleteReplyRequest(BaseModel):
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
        reply_resp = requests.delete(reply_url, headers=headers)
        
        if reply_resp.ok:
            return {"status": "success", "message": "Reply deleted successfully!"}
        else:
            return {"status": "error", "message": f"Google refused to delete reply: {reply_resp.text}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class PublishPostRequest(BaseModel):
    provider_token: str
    location_id: str
    summary: str
    image_url: str = None
    post_type: str = "LOCAL_POST"

@app.post("/api/google/publish-post")
async def publish_local_post(req: PublishPostRequest):
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        acc_url = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
        acc_resp = requests.get(acc_url, headers=headers)
        if not acc_resp.ok:
            return {"status": "error", "message": f"Google Account Fetch Error: {acc_resp.text}"}
        accounts = acc_resp.json().get('accounts', [])
        if not accounts:
            return {"status": "error", "message": "No Google Business Accounts found."}
        account_name = accounts[0]['name']
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
                
            resp = requests.post(url, headers=headers, json=payload)
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
                
            resp = requests.post(url, headers=headers, json=payload)
        
        if not resp.ok:
            return {"status": "error", "message": f"Google refused post: {resp.text}"}
            
        return {"status": "success", "post_data": resp.json()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
        
        resp = requests.get(url, headers=headers, params=params)
        
        if not resp.ok:
            return {"status": "error", "message": f"Analytics Fetch Error: {resp.text}"}
            
        return {"status": "success", "analytics": resp.json()}
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        return {"status": "error", "message": f"Backend Crash: {str(e)} | Trace: {error_details}"}


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
            
        url = f"https://businessprofileperformance.googleapis.com/v1/{clean_loc}/searchkeywords/impressions/monthly"
        resp = requests.get(url, headers=headers, params=params)
        
        if not resp.ok:
            return {"status": "error", "message": resp.text}
            
        return {"status": "success", "keywords": resp.json().get("searchKeywordsMonthlyImpressions", [])}
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        return {"status": "error", "message": f"Backend Crash: {str(e)} | Trace: {error_details}"}

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
        posts = supabase.table('calendar_posts')\
            .select('*')\
            .eq('status', 'published')\
            .lt('post_date', yesterday)\
            .not_is('image_url', 'null')\
            .execute()
            
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
                    supabase.table('calendar_posts').update({'image_url': None}).eq('id', p['id']).execute()
                    deleted_count += 1
                    
        return {"status": "success", "message": f"Scrubbed {deleted_count} heavy images to save space."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

class RefreshTokenRequest(BaseModel):
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
            
        user_data = supabase.auth.admin.get_user_by_id(req.user_id)
        if not user_data.user:
            return {"status": "error", "message": "User not found"}
            
        refresh_token = user_data.user.user_metadata.get('google_refresh_token')
        if not refresh_token:
            return {"status": "error", "message": "No refresh token found for this user"}
            
        new_access_token = get_offline_access_token(refresh_token)
        return {"status": "success", "provider_token": new_access_token}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/cron/publish-scheduled")
async def publish_scheduled_posts():
    """
    Runs every morning. Finds today's scheduled posts, securely refreshes token, 
    and publishes to Google.
    """
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        posts = supabase.table('calendar_posts').select('*').eq('status', 'scheduled').lte('post_date', today).execute()
        
        published = 0
        for post in posts.data:
            user = supabase.auth.admin.get_user_by_id(post['user_id'])
            refresh_token = user.user.user_metadata.get('google_refresh_token')
            if not refresh_token:
                continue
                
            try:
                access_token = get_offline_access_token(refresh_token)
                
                # Re-use our existing logic
                req = PublishPostRequest(
                    provider_token=access_token,
                    location_id=post['location_id'],
                    summary=post.get('caption', ''),
                    image_url=post.get('image_url'),
                    post_type=post.get('post_type', 'LOCAL_POST')
                )
                res = await publish_local_post(req)
                
                if res.get('status') == 'success':
                    supabase.table('calendar_posts').update({'status': 'published'}).eq('id', post['id']).execute()
                    published += 1
            except Exception as e:
                print(f"Failed to auto-publish post {post['id']}: {e}")
                
        return {"status": "success", "published": published}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/cron/daily-backlog-reviews")
async def daily_backlog_reviews():
    """
    Runs once a day. Scans all active users and replies to up to 4 old/backlogged reviews per location 
    to slowly catch up without triggering Google's spam filters.
    """
    try:
        users = supabase.auth.admin.list_users()
        total_replies_sent = 0
        
        for u in users:
            meta = u.user_metadata or {}
            refresh_token = meta.get('google_refresh_token')
            subs = meta.get('subscriptions', {})
            
            if not refresh_token or not subs:
                continue
                
            try:
                access_token = get_offline_access_token(refresh_token)
                
                for loc_id, sub_data in subs.items():
                    if sub_data.get('status') == 'active':
                        # loc_id in db is usually just "12345" or "locations/12345"
                        clean_loc_id = loc_id.replace('locations/', '')
                        
                        req = GoogleReviewRequest(
                            provider_token=access_token,
                            location_id=clean_loc_id,
                            user_id=u.id
                        )
                        res = await sync_and_reply_reviews(req)
                        
                        # Just counting successfully sent replies from the response string
                        if res.get('status') == 'success' and 'AI sent' in res.get('message', ''):
                            try:
                                num = int(res['message'].split('AI sent ')[1].split(' ')[0])
                                total_replies_sent += num
                            except:
                                pass
            except Exception as e:
                print(f"Failed backlog review sync for user {u.id}: {e}")
                
        return {"status": "success", "message": f"Daily backlog completed. Sent {total_replies_sent} replies."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

import base64
import json

@app.post("/api/webhooks/google-reviews")
async def google_reviews_webhook(req: Request):
    """
    Receives real-time push notifications from Google Cloud Pub/Sub
    when a new review is posted.
    """
    try:
        body = await req.json()
        message = body.get('message', {})
        data_b64 = message.get('data')
        
        if not data_b64:
            return {"status": "ignored", "reason": "No data field"}
            
        decoded_bytes = base64.b64decode(data_b64)
        payload = json.loads(decoded_bytes.decode('utf-8'))
        
        location_name = payload.get('locationName') # e.g. accounts/123/locations/456
        review_name = payload.get('reviewName')
        
        if not location_name or not review_name:
            return {"status": "ignored", "reason": "Missing location or review name"}
            
        # Extract just the "locations/456" part to match our DB
        loc_id_short = "locations/" + location_name.split('locations/')[-1]
        
        # 1. Find the user who owns this location
        users = supabase.auth.admin.list_users()
        target_user = None
        refresh_token = None
        
        for u in users:
            meta = u.user_metadata or {}
            subs = meta.get('subscriptions', {})
            if loc_id_short in subs and subs[loc_id_short].get('status') == 'active':
                target_user = u
                refresh_token = meta.get('google_refresh_token')
                break
                
        if not target_user or not refresh_token:
            return {"status": "ignored", "reason": "Location not actively subscribed or missing token"}
            
        # 2. Get Access Token
        access_token = get_offline_access_token(refresh_token)
        headers = {"Authorization": f"Bearer {access_token}"}
        
        # 3. Fetch the exact review
        rev_url = f"https://mybusiness.googleapis.com/v4/{review_name}"
        rev_resp = requests.get(rev_url, headers=headers)
        if not rev_resp.ok:
            return {"status": "error", "reason": "Failed to fetch review"}
            
        review_data = rev_resp.json()
        
        # If already replied, skip
        if 'reviewReply' in review_data:
            return {"status": "ignored", "reason": "Already replied"}
            
        # Fetch target keywords from Supabase metadata
        target_keywords = []
        if supabase and target_user:
            try:
                all_settings = (target_user.user_metadata or {}).get("ai_settings", {})
                loc_settings = all_settings.get(loc_id_short, {})
                target_keywords = loc_settings.get('active_keywords', [])
            except Exception as e:
                print("Error fetching keywords from metadata:", e)
                
        keyword_instruction = ""
        if target_keywords:
            keyword_list = ", ".join([f'"{k}"' for k in target_keywords])
            keyword_instruction = f"IMPORTANT: Organically and naturally inject one of these SEO keywords into the reply: {keyword_list}. Do NOT sound like a robot."
            
        customer_comment = review_data.get('comment', '').strip()
        rating = review_data.get('starRating')
        if not customer_comment:
            prompt = f"A customer just left a {rating}-star rating with NO text. Write a professional and extremely short, creative 'Thank you' reply (max 2 sentences) appreciating their rating. {keyword_instruction} Do not include placeholders."
        else:
            prompt = f"Write a professional and extremely short reply (max 2 sentences) to this customer review. Customer Rating: {rating}. Customer Comment: '{customer_comment}'. {keyword_instruction} Do not include placeholders."
        
        ai_reply = generate_ai_reply(prompt)
        
        # 5. Post Reply
        reply_url = f"https://mybusiness.googleapis.com/v4/{review_name}/reply"
        reply_resp = requests.put(reply_url, headers=headers, json={"comment": ai_reply})
        
        if reply_resp.ok:
            return {"status": "success", "message": "Instantly replied to review!"}
        else:
            return {"status": "error", "reason": reply_resp.text}
            
    except Exception as e:
        print(f"Webhook error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# COMPETITORS API
# ==========================================

class CompetitorRequest(BaseModel):
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
        # Fallback realistic mock data if the API key isn't provided yet
        return {
            "status": "success",
            "competitors": [
                {"name": "Sharma Plumbing & AC", "rating": 4.3, "user_ratings_total": 112, "top_review": "They fixed my pipes but were 2 hours late."},
                {"name": "Delhi Quick Fix", "rating": 4.1, "user_ratings_total": 84, "top_review": "Decent service, a bit expensive."},
                {"name": "Metro AC Repairs", "rating": 3.9, "user_ratings_total": 45, "top_review": "AC broke down again after a week."}
            ]
        }
        
    try:
        search_url = f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={req.keyword} near {req.location_name}&key={maps_key}"
        resp = requests.get(search_url)
        if not resp.ok:
            raise HTTPException(status_code=500, detail="Google Places API failed")
            
        data = resp.json()
        places = data.get('results', [])[:3] # Top 3 competitors
        
        competitors = []
        for p in places:
            # Fetch details to get the top text review
            place_id = p.get('place_id')
            details_url = f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&fields=name,rating,user_ratings_total,reviews&key={maps_key}"
            det_resp = requests.get(details_url)
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


