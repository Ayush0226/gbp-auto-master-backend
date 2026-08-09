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
    'monthly': {'original': 360, 'discounted': 289},
    'half_yearly': {'original': 2160, 'discounted': 1649},
    'yearly': {'original': 4380, 'discounted': 3149}
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
    final_price = base_price
    
    if req.promo_code == 'FIRSTUNDER10':
        final_price = PRICING_PLANS[req.plan_id]['discounted']
    elif req.promo_code == 'ATYAUNSUHJ':
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
                import datetime
                now = datetime.datetime.now()
                months = 1 if req.plan_id == 'monthly' else (6 if req.plan_id == 'half_yearly' else 12)
                expires_at = (now + datetime.timedelta(days=30*months)).isoformat()
                
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
        "amount": final_price * 100, # Subunits
        "currency": "INR",
        "receipt": f"receipt_{req.user_id[:8]}",
        "notes": {
            "plan_id": req.plan_id,
            "user_id": req.user_id,
            "location_id": req.location_id
        }
    }
    
    try:
        order = razorpay_client.order.create(data=data)
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
        
        razorpay_client.utility.verify_payment_signature(params_dict)
        
        # If we get here, signature is valid! Securely update Supabase.
        if not supabase:
            raise HTTPException(status_code=500, detail="Supabase not configured")
            
        # Fetch current metadata to append location
        user_data = supabase.auth.admin.get_user_by_id(req.user_id)
        user_meta = user_data.user.user_metadata if user_data.user else {}
        subs = user_meta.get("subscriptions", {})
        
        if req.location_id and req.plan_id:
            import datetime
            now = datetime.datetime.now()
            months = 1 if req.plan_id == 'monthly' else (6 if req.plan_id == 'half' else 12)
            expires_at = (now + datetime.timedelta(days=30*months)).isoformat()
            
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
        
@app.get("/api/payment/key")
async def get_razorpay_key():
    return {"key": os.getenv("RAZORPAY_KEY_ID", "")}

@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "service": "gbp-auto-master-backend"}

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
groq_api_key = os.getenv('GROQ_API_KEY', '')
def generate_ai_reply(api_key: str, prompt: str) -> str:
    """
    Generates a reply using Groq's Llama 3 model (100% free and lightning fast).
    """
    client = Groq(api_key=groq_api_key)
    
    chat_completion = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": "You are a professional customer service AI. Write extremely short (max 2 sentences) and polite replies to customer reviews."
            },
            {
                "role": "user",
                "content": prompt,
            }
        ],
        model="llama3-8b-8192",
    )
    return chat_completion.choices[0].message.content

class GoogleSyncRequest(BaseModel):
    user_id: str
    provider_token: str = None

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
            
        data = resp.json().get('reviews', [])
        
        # Format the reviews for the frontend
        formatted_reviews = []
        for r in data[:10]: # Return top 10
            formatted_reviews.append({
                "id": r.get('name'),
                "reviewer": r.get('reviewer', {}).get('displayName', 'Anonymous'),
                "rating": r.get('starRating', 'FIVE'),
                "comment": r.get('comment', ''),
                "createTime": r.get('createTime', ''),
                "has_reply": 'reviewReply' in r
            })
            
        return {"status": "success", "reviews": formatted_reviews}
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
        
        # Find unreplied reviews
        unreplied = [r for r in reviews if 'reviewReply' not in r and r.get('comment')]
        
        replies_sent = 0
        
        for r in unreplied:
            try:
                prompt = f"Write a professional and extremely short reply (max 2 sentences) to this customer review. Customer Rating: {r.get('starRating')}. Customer Comment: '{r.get('comment')}'. Do not include placeholders."
                
                ai_reply = generate_ai_reply("dummy_param", prompt)
                
                # Post reply back to Google API
                reply_url = f"https://mybusiness.googleapis.com/v4/{r.get('name')}/reply"
                reply_resp = requests.put(reply_url, headers=headers, json={"comment": ai_reply})
                
                if reply_resp.ok:
                    replies_sent += 1
                else:
                    return {"status": "error", "message": f"Google refused reply: {reply_resp.text}"}
            except Exception as inner_e:
                return {"status": "error", "message": f"Gemini Error on review {r.get('name')}: {str(inner_e)}"}
                
        return {
            "status": "success",
            "message": f"Successfully synced. AI sent {replies_sent} replies."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class PublishPostRequest(BaseModel):
    provider_token: str
    location_id: str
    summary: str
    image_url: str = None

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
        
        if not resp.ok:
            return {"status": "error", "message": f"Google refused post: {resp.text}"}
            
        return {"status": "success", "post_data": resp.json()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# ANALYTICS & SEO
# ==========================================

@app.post("/api/google/analytics")
async def get_google_analytics(req: GoogleReviewRequest):
    try:
        headers = {"Authorization": f"Bearer {req.provider_token}"}
        
        # Performance API uses locations/12345 (NO account prefix)
        url = f"https://businessprofileperformance.googleapis.com/v1/{req.location_id}:fetchMultiDailyMetricsTimeSeries"
        
        # Get metrics for the last 30 days
        import datetime
        end_date = datetime.datetime.now()
        start_date = end_date - datetime.timedelta(days=30)
        
        params = {
            "dailyMetrics": ["WEBSITE_CLICKS", "CALL_CLICKS", "BUSINESS_DIRECTION_REQUESTS", "BUSINESS_IMPRESSIONS_DESKTOP_MAPS", "BUSINESS_IMPRESSIONS_MOBILE_MAPS"],
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
        raise HTTPException(status_code=500, detail=str(e))

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
                    image_url=post.get('image_url')
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
            
        # 4. Generate AI Reply
        prompt = f"Write a professional and extremely short reply (max 2 sentences) to this customer review. Customer Rating: {review_data.get('starRating')}. Customer Comment: '{review_data.get('comment', '')}'. Do not include placeholders."
        
        ai_reply = generate_ai_reply("dummy_param", prompt)
        
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


