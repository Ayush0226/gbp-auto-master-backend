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

@app.post("/api/google/locations")
async def get_google_locations(req: GoogleSyncRequest):
    """
    Fetches the user's provider token from Supabase,
    and calls the Google Business Profile API to list their locations.
    """
    if not supabase:
        raise HTTPException(status_code=500, detail="Database not configured")
        
    try:
        # 1. Fetch user's Google token from Supabase Auth
        user_response = supabase.auth.admin.get_user_by_id(req.user_id)
        # Note: In a real production setup, we would fetch the stored provider_token 
        # or use a securely stored refresh_token to get a fresh access_token.
        # token = fetch_secure_token(req.user_id)
        
        # 2. Call Google Business Profile API
        # url = f"https://mybusinessbusinessinformation.googleapis.com/v1/accounts/{account_id}/locations"
        # headers = {"Authorization": f"Bearer {token}"}
        # response = requests.get(url, headers=headers)
        
        # 3. For now, since the API is not yet approved in Google Cloud Console,
        # we return a structured error guiding the user.
        return {
            "status": "pending_setup", 
            "message": "Google API access is not yet configured in Google Cloud Console.",
            "locations": []
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

