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
            # Updating user metadata using admin role to bypass security policies
            supabase.auth.admin.update_user_by_id(req.user_id, user_metadata={"subscription_status": "active"})
            return {"status": "success", "message": "Free trial activated successfully", "free_trial": True}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
    # Generate Razorpay Order
    data = {
        "amount": final_price * 100, # Subunits
        "currency": "INR",
        "receipt": f"receipt_{req.user_id[:8]}",
        "notes": {
            "plan_id": req.plan_id,
            "user_id": req.user_id
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
            
        supabase.auth.admin.update_user_by_id(req.user_id, user_metadata={"subscription_status": "active"})
        
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
    # In the future, this endpoint will trigger the background review sync for all active users
    return {"status": "healthy", "service": "gbp-auto-master-backend"}


# ==========================================
# GOOGLE BUSINESS PROFILE & AI ENGINE
# ==========================================

import requests
import google.generativeai as genai
import itertools

# We initialize Gemini to support multiple rotating keys for the free tier
gemini_keys_env = os.getenv('GEMINI_API_KEYS', '')
gemini_keys = [k.strip() for k in gemini_keys_env.split(',')] if gemini_keys_env else []
key_cycle = itertools.cycle(gemini_keys) if gemini_keys else None

def get_next_gemini_key():
    if not key_cycle:
        return None
    return next(key_cycle)

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
        
        # 3. Format for dashboard
        dashboard_locations = []
        for loc in google_locations:
            dashboard_locations.append({
                "id": loc.get("name"), # e.g. "locations/12345"
                "name": loc.get("title", "Unnamed Location"),
                "reviews": 0, 
                "rating": 0.0,
                "subscribed": False 
            })
            
        return {
            "status": "success", 
            "locations": dashboard_locations
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/google/sync-reviews")
async def sync_and_reply_reviews(req: GoogleSyncRequest):
    """
    Fetches unreplied reviews, uses Gemini to generate SEO-optimized replies, 
    and posts them back to Google.
    """
    # 1. Verify user subscription status
    # 2. Fetch unreplied reviews via Google API
    
    # 3. Use Gemini to generate reply using key rotation:
    # api_key = get_next_gemini_key()
    # if not api_key:
    #     raise HTTPException(status_code=500, detail="Gemini API keys not configured")
    # genai.configure(api_key=api_key)
    # gemini_model = genai.GenerativeModel('gemini-1.5-flash')
    # prompt = "Write a professional reply to this 5-star review. Mention 'emergency plumber'."
    # ai_reply = gemini_model.generate_content(prompt)
    
    # 4. Post reply back to Google API
    
    return {
        "status": "pending_setup",
        "message": "Gemini AI Engine (with key rotation) is standing by. Waiting for Google API approval."
    }

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
    and updates the database row to image_url=null (leaving the text history intact).
    """
    if not supabase:
        raise HTTPException(status_code=500, detail="Database not configured")
        
    try:
        # Calculate yesterday's date
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
        
        # 1. Find all published posts from yesterday or older that still have an image attached
        response = supabase.table('calendar_posts').select('id, image_url').eq('status', 'published').lt('post_date', yesterday).not_.is_('image_url', 'null').execute()
        
        posts_to_scrub = response.data
        if not posts_to_scrub:
            return {"status": "success", "message": "No old images to scrub today.", "scrubbed_count": 0}
            
        scrubbed_count = 0
        
        for post in posts_to_scrub:
            image_url = post['image_url']
            # The image_url is usually a public URL. We need to extract the exact file path from it.
            # Example: https://[project].supabase.co/storage/v1/object/public/calendar_images/user123/img.jpg
            # We extract just the path after the bucket name: "user123/img.jpg"
            if 'calendar_images/' in image_url:
                file_path = image_url.split('calendar_images/')[-1]
                
                # 2. Delete the heavy file from Supabase Storage
                supabase.storage.from_('calendar_images').remove([file_path])
                
                # 3. Update the database row to remove the URL (the text caption stays!)
                supabase.table('calendar_posts').update({'image_url': None}).eq('id', post['id']).execute()
                
                scrubbed_count += 1
                
        return {"status": "success", "message": f"Successfully scrubbed {scrubbed_count} old images to save storage space.", "scrubbed_count": scrubbed_count}
        
    except Exception as e:
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


