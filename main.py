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
    return {"status": "healthy", "service": "gbp-auto-master-backend"}
