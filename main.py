"""
TryOn Backend v6 - LightX API powered virtual try-on
Free tier: 25 credits on signup, no credit card needed
"""
import os, base64, time, json, asyncio
import httpx, stripe
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
from collections import defaultdict
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

app = FastAPI(title="TryOn API", version="6.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
    allow_methods=["POST","GET","OPTIONS"], allow_headers=["Content-Type","stripe-signature"])

PLANS = {
    "free":    {"monthly_limit": None, "lifetime_limit": 5,  "price_inr": 0},
    "starter": {"monthly_limit": 8,   "lifetime_limit": None,"price_inr": 49},
    "basic":   {"monthly_limit": 20,  "lifetime_limit": None,"price_inr": 99},
    "pro":     {"monthly_limit": 45,  "lifetime_limit": None,"price_inr": 199},
    "power":   {"monthly_limit": 80,  "lifetime_limit": None,"price_inr": 349},
}

users_db: dict = {}
_ip_calls: dict = defaultdict(list)
_global = {"daily_calls": 0, "day_start": time.time()}
GLOBAL_DAILY_CAP = 500

def check_rate_limit(ip: str):
    now = time.time()
    if now - _global["day_start"] > 86400:
        _global["daily_calls"] = 0
        _global["day_start"] = now
        _ip_calls.clear()
    if _global["daily_calls"] >= GLOBAL_DAILY_CAP:
        raise HTTPException(503, "Service at daily capacity.")
    history = _ip_calls[ip]
    one_hour_ago = now - 3600
    one_day_ago  = now - 86400
    recent_hour = [t for t in history if t > one_hour_ago]
    recent_day  = [t for t in history if t > one_day_ago]
    if len(recent_hour) >= 15:
        raise HTTPException(429, "Too many requests. Max 15/hour.")
    if len(recent_day) >= 30:
        raise HTTPException(429, "Daily limit reached.")
    _ip_calls[ip] = recent_day + [now]
    _global["daily_calls"] += 1

def get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd: return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

class TryOnRequest(BaseModel):
    person_image: str
    cloth_image_url: str
    platform: str = "amazon"
    user_email: str = ""

    @validator("person_image")
    def validate_image(cls, v):
        if not v.startswith("data:image/"): raise ValueError("Must be base64 data URL")
        if len(v) > 20 * 1024 * 1024: raise ValueError("Image too large")
        return v

    @validator("cloth_image_url")
    def validate_url(cls, v):
        if not v.startswith("http"): raise ValueError("Must be valid HTTP URL")
        return v

class CheckoutRequest(BaseModel):
    email: str
    plan: str

class EmailRequest(BaseModel):
    email: str

def get_user(email: str) -> dict:
    key = email.strip().lower()
    if key not in users_db:
        users_db[key] = {"subscription":"free","lifetime_tries":0,"monthly_tries":0,
                         "month_start":time.time(),"stripe_customer_id":None}
    return users_db[key]

def reset_monthly_if_needed(user: dict):
    if time.time() - user["month_start"] > 30 * 86400:
        user["monthly_tries"] = 0
        user["month_start"] = time.time()

def can_try_on(user: dict) -> tuple:
    plan_name = user["subscription"]
    plan = PLANS.get(plan_name, PLANS["free"])
    if plan_name == "free":
        used = user["lifetime_tries"]
        limit = plan["lifetime_limit"]
        if used >= limit: return False, "upgrade_required", 0
        return True, "free", max(0, limit - used)
    reset_monthly_if_needed(user)
    used = user["monthly_tries"]
    limit = plan["monthly_limit"]
    if used >= limit: return False, "monthly_limit_reached", 0
    return True, "ok", max(0, limit - used)

@app.get("/")
def root(): return {"status":"ok","service":"TryOn API v6.0"}

@app.get("/health")
def health(): return {"status":"healthy","global_calls_today":_global["daily_calls"],"global_daily_cap":GLOBAL_DAILY_CAP}

@app.post("/api/user/status")
async def user_status(req: EmailRequest):
    user = get_user(req.email)
    reset_monthly_if_needed(user)
    plan_name = user["subscription"]
    plan = PLANS.get(plan_name, PLANS["free"])
    if plan_name == "free":
        used = user["lifetime_tries"]; limit = plan["lifetime_limit"]
    else:
        used = user["monthly_tries"]; limit = plan["monthly_limit"]
    return {"subscription":plan_name,"tries_used":used,"tries_limit":limit,
            "tries_remaining":max(0,limit-used),"is_paid":plan_name!="free","plan_price_inr":plan["price_inr"]}

@app.post("/api/create-checkout")
async def create_checkout(req: CheckoutRequest):
    if not stripe.api_key: raise HTTPException(500,"Stripe not configured")
    price_map = {"starter":os.getenv("STRIPE_PRICE_STARTER",""),"basic":os.getenv("STRIPE_PRICE_BASIC",""),
                 "pro":os.getenv("STRIPE_PRICE_PRO",""),"power":os.getenv("STRIPE_PRICE_POWER","")}
    price_id = price_map.get(req.plan)
    if not price_id: raise HTTPException(400,f"Unknown plan: {req.plan}")
    frontend_url = os.getenv("FRONTEND_URL","https://yoursite.netlify.app").rstrip("/")
    try:
        user = get_user(req.email)
        customer_id = user.get("stripe_customer_id")
        if not customer_id:
            customer = stripe.Customer.create(email=req.email)
            customer_id = customer.id
            user["stripe_customer_id"] = customer_id
        session = stripe.checkout.Session.create(
            customer=customer_id, payment_method_types=["card"],
            line_items=[{"price":price_id,"quantity":1}], mode="subscription",
            success_url=f"{frontend_url}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{frontend_url}/pricing.html",
            metadata={"email":req.email,"plan":req.plan}, allow_promotion_codes=True)
        return {"checkout_url":session.url}
    except stripe.error.StripeError as e:
        raise HTTPException(500,f"Payment error: {str(e)}")

@app.post("/api/manage-subscription")
async def manage_subscription(req: EmailRequest):
    user = get_user(req.email)
    customer_id = user.get("stripe_customer_id")
    if not customer_id: raise HTTPException(404,"No subscription found")
    frontend_url = os.getenv("FRONTEND_URL","https://yoursite.netlify.app")
    try:
        portal = stripe.billing_portal.Session.create(customer=customer_id,return_url=f"{frontend_url}/pricing.html")
        return {"portal_url":portal.url}
    except stripe.error.StripeError as e:
        raise HTTPException(500,str(e))

@app.post("/api/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    secret = os.getenv("STRIPE_WEBHOOK_SECRET","")
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, secret)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400,"Invalid signature")
    etype = event["type"]; obj = event["data"]["object"]
    if etype == "checkout.session.completed":
        email = obj.get("metadata",{}).get("email","")
        plan  = obj.get("metadata",{}).get("plan","starter")
        if email:
            user = get_user(email)
            user["subscription"] = plan
            user["monthly_tries"] = 0
            user["month_start"] = time.time()
    elif etype in ("customer.subscription.deleted","invoice.payment_failed"):
        cid = obj.get("customer")
        for u in users_db.values():
            if u.get("stripe_customer_id") == cid:
                u["subscription"] = "free"; break
    return {"received":True}

@app.post("/api/tryon")
async def try_on(request_data: TryOnRequest, request: Request):
    start = time.time()
    lightx_key = os.getenv("LIGHTX_API_KEY","")
    if not lightx_key:
        raise HTTPException(500,"LIGHTX_API_KEY not configured on server")

    check_rate_limit(get_client_ip(request))

    user_email = request_data.user_email.strip().lower()
    user = get_user(user_email) if user_email else None
    if user:
        allowed, reason, remaining = can_try_on(user)
        if not allowed:
            frontend_url = os.getenv("FRONTEND_URL","")
            raise HTTPException(402, json.dumps({
                "error": reason,
                "message": "You've used all free try-ons. Upgrade to continue.",
                "upgrade_url": f"{frontend_url}/pricing.html",
                "tries_remaining": 0,
            }))

    try:
        # Step 1: Upload person image to LightX
        logger.info("Uploading person image to LightX...")
        headers = {"x-api-key": lightx_key, "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get upload URL for person image
            upload_resp = await client.post(
                "https://api.lightxeditor.com/external/api/v1/uploadImageUrl",
                headers=headers,
                json={"uploadType": "imageUrl", "size": len(request_data.person_image)}
            )
            if upload_resp.status_code != 200:
                logger.error(f"LightX upload error: {upload_resp.text}")
                raise HTTPException(500, "Failed to prepare image upload")

            upload_data = upload_resp.json()
            model_image_url = upload_data.get("data", {}).get("imageUrl", "")
            upload_url = upload_data.get("data", {}).get("uploadUrl", "")

            if not upload_url:
                raise HTTPException(500, "No upload URL from LightX")

            # Upload person image bytes
            _, encoded = request_data.person_image.split(",", 1)
            person_bytes = base64.b64decode(encoded)

            put_resp = await client.put(
                upload_url,
                content=person_bytes,
                headers={"Content-Type": "image/jpeg"}
            )
            if put_resp.status_code not in (200, 201):
                raise HTTPException(500, "Failed to upload person image")

        # Step 2: Run virtual try-on
        logger.info("Running LightX virtual try-on...")
        async with httpx.AsyncClient(timeout=30.0) as client:
            tryon_resp = await client.post(
                "https://api.lightxeditor.com/external/api/v1/virtualTryOn",
                headers=headers,
                json={
                    "modelImageUrl": model_image_url,
                    "clothImageUrl": request_data.cloth_image_url,
                    "clothType": "upper",
                }
            )
            if tryon_resp.status_code != 200:
                logger.error(f"LightX tryon error: {tryon_resp.text}")
                raise HTTPException(500, "Try-on request failed")

            tryon_data = tryon_resp.json()
            order_id = tryon_data.get("data", {}).get("orderId", "")
            if not order_id:
                raise HTTPException(500, "No order ID from LightX")

        # Step 3: Poll for result
        logger.info(f"Polling for result, order: {order_id}")
        async with httpx.AsyncClient(timeout=10.0) as client:
            for attempt in range(40):
                await asyncio.sleep(3)
                status_resp = await client.post(
                    "https://api.lightxeditor.com/external/api/v1/order-status",
                    headers=headers,
                    json={"orderId": order_id}
                )
                status_data = status_resp.json()
                status = status_data.get("data", {}).get("status", "")
                logger.info(f"LightX status [{attempt}]: {status}")

                if status == "active":
                    result_url = status_data.get("data", {}).get("output", "")
                    if not result_url:
                        raise HTTPException(500, "No output image from LightX")

                    if user:
                        if user["subscription"] == "free":
                            user["lifetime_tries"] = user.get("lifetime_tries", 0) + 1
                        else:
                            user["monthly_tries"] = user.get("monthly_tries", 0) + 1

                    _, _, remaining_after = can_try_on(user) if user else (True, "ok", 999)
                    elapsed = round(time.time() - start, 2)
                    logger.info(f"Try-on done in {elapsed}s")
                    return {
                        "result_url": result_url,
                        "processing_time": elapsed,
                        "subscription": user["subscription"] if user else "anonymous",
                        "tries_remaining": remaining_after,
                    }

                elif status in ("failed", "error"):
                    raise HTTPException(500, "Try-on processing failed")

            raise HTTPException(408, "Try-on timed out. Please try again.")

    except HTTPException: raise
    except httpx.TimeoutException:
        raise HTTPException(408, "Request timed out. Please try again.")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        raise HTTPException(500, "Internal server error")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
