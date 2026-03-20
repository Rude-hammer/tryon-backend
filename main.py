"""
TryOn Backend v4 — Multi-tier subscriptions, always profitable
Plans: Free(5 total) | Starter ₹49(8/mo) | Basic ₹99(20/mo) | Pro ₹199(45/mo) | Power ₹349(80/mo)
"""

import os, base64, time, json
import httpx, replicate, stripe
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator as validator
from collections import defaultdict
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

app = FastAPI(title="TryOn API", version="4.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "stripe-signature"],
)

# ─── PLAN DEFINITIONS ─────────────────────────────────────────────────────────
# monthly_limit = None means free lifetime tries (not monthly reset)
PLANS = {
    "free":     {"monthly_limit": None, "lifetime_limit": 5,  "price_inr": 0},
    "starter":  {"monthly_limit": 8,   "lifetime_limit": None,"price_inr": 49},
    "basic":    {"monthly_limit": 20,  "lifetime_limit": None,"price_inr": 99},
    "pro":      {"monthly_limit": 45,  "lifetime_limit": None,"price_inr": 199},
    "power":    {"monthly_limit": 80,  "lifetime_limit": None,"price_inr": 349},
}

# ─── IN-MEMORY DB ─────────────────────────────────────────────────────────────
users_db: dict = {}

# ─── RATE LIMITER ─────────────────────────────────────────────────────────────
_ip_calls: dict = defaultdict(list)
_global = {"daily_calls": 0, "day_start": time.time()}
GLOBAL_DAILY_CAP = 500  # hard stop — max $25/day exposure

def check_rate_limit(ip: str):
    now = time.time()
    if now - _global["day_start"] > 86400:
        _global["daily_calls"] = 0
        _global["day_start"] = now
        _ip_calls.clear()

    if _global["daily_calls"] >= GLOBAL_DAILY_CAP:
        raise HTTPException(503, "Service at daily capacity. Try again tomorrow.")

    history = _ip_calls[ip]
    one_hour_ago = now - 3600
    one_day_ago  = now - 86400
    recent_hour  = [t for t in history if t > one_hour_ago]
    recent_day   = [t for t in history if t > one_day_ago]

    if len(recent_hour) >= 15:
        raise HTTPException(429, "Too many requests. Max 15 try-ons per hour per device.")
    if len(recent_day) >= 30:
        raise HTTPException(429, "Daily device limit reached.")

    _ip_calls[ip] = recent_day + [now]
    _global["daily_calls"] += 1

def get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

# ─── MODELS ───────────────────────────────────────────────────────────────────
class TryOnRequest(BaseModel):
    person_image: str
    cloth_image_url: str
    platform: str = "amazon"
    user_email: str = ""

    @validator("person_image")
    def validate_image(cls, v):
        if not v.startswith("data:image/"):
            raise ValueError("Must be base64 data URL")
        if len(v) > 20 * 1024 * 1024:
            raise ValueError("Image too large")
        return v

    @validator("cloth_image_url")
    def validate_url(cls, v):
        if not v.startswith("http"):
            raise ValueError("Must be valid HTTP URL")
        return v

class CheckoutRequest(BaseModel):
    email: str
    plan: str

class EmailRequest(BaseModel):
    email: str

# ─── USER HELPERS ─────────────────────────────────────────────────────────────
def get_user(email: str) -> dict:
    key = email.strip().lower()
    if key not in users_db:
        users_db[key] = {
            "subscription": "free",
            "lifetime_tries": 0,       # for free plan
            "monthly_tries": 0,        # for paid plans
            "month_start": time.time(),# when monthly counter started
            "stripe_customer_id": None,
        }
    return users_db[key]

def reset_monthly_if_needed(user: dict):
    """Reset monthly counter if a new month has started."""
    now = time.time()
    if now - user["month_start"] > 30 * 86400:
        user["monthly_tries"] = 0
        user["month_start"] = now

def can_try_on(user: dict) -> tuple:
    plan_name = user["subscription"]
    plan = PLANS.get(plan_name, PLANS["free"])

    if plan_name == "free":
        used = user["lifetime_tries"]
        limit = plan["lifetime_limit"]
        remaining = max(0, limit - used)
        if used >= limit:
            return False, "upgrade_required", 0
        return True, "free", remaining

    # Paid plan — check monthly limit
    reset_monthly_if_needed(user)
    used = user["monthly_tries"]
    limit = plan["monthly_limit"]
    remaining = max(0, limit - used)
    if used >= limit:
        return False, "monthly_limit_reached", 0
    return True, "ok", remaining

# ─── HEALTH ───────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "TryOn API v4.0"}

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "global_calls_today": _global["daily_calls"],
        "global_daily_cap": GLOBAL_DAILY_CAP,
    }

# ─── USER STATUS ──────────────────────────────────────────────────────────────
@app.post("/api/user/status")
async def user_status(req: EmailRequest):
    user = get_user(req.email)
    reset_monthly_if_needed(user)
    plan_name = user["subscription"]
    plan = PLANS.get(plan_name, PLANS["free"])

    if plan_name == "free":
        used = user["lifetime_tries"]
        limit = plan["lifetime_limit"]
        remaining = max(0, limit - used)
    else:
        used = user["monthly_tries"]
        limit = plan["monthly_limit"]
        remaining = max(0, limit - used)

    return {
        "subscription": plan_name,
        "tries_used": used,
        "tries_limit": limit,
        "tries_remaining": remaining,
        "is_paid": plan_name != "free",
        "plan_price_inr": plan["price_inr"],
    }

# ─── STRIPE CHECKOUT ──────────────────────────────────────────────────────────
@app.post("/api/create-checkout")
async def create_checkout(req: CheckoutRequest):
    if not stripe.api_key:
        raise HTTPException(500, "Stripe not configured")

    # Map plan names to Stripe Price IDs from env vars
    price_map = {
        "starter": os.getenv("STRIPE_PRICE_STARTER", ""),
        "basic":   os.getenv("STRIPE_PRICE_BASIC", ""),
        "pro":     os.getenv("STRIPE_PRICE_PRO", ""),
        "power":   os.getenv("STRIPE_PRICE_POWER", ""),
    }
    price_id = price_map.get(req.plan)
    if not price_id:
        raise HTTPException(400, f"Unknown plan or price not configured: {req.plan}")

    frontend_url = os.getenv("FRONTEND_URL", "https://yoursite.netlify.app").rstrip("/")

    try:
        user = get_user(req.email)
        customer_id = user.get("stripe_customer_id")
        if not customer_id:
            customer = stripe.Customer.create(email=req.email)
            customer_id = customer.id
            user["stripe_customer_id"] = customer_id

        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{frontend_url}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{frontend_url}/pricing.html",
            metadata={"email": req.email, "plan": req.plan},
            allow_promotion_codes=True,
        )
        return {"checkout_url": session.url}
    except stripe.error.StripeError as e:
        raise HTTPException(500, f"Payment error: {str(e)}")

# ─── MANAGE SUBSCRIPTION ──────────────────────────────────────────────────────
@app.post("/api/manage-subscription")
async def manage_subscription(req: EmailRequest):
    user = get_user(req.email)
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(404, "No subscription found")
    frontend_url = os.getenv("FRONTEND_URL", "https://yoursite.netlify.app")
    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{frontend_url}/pricing.html",
        )
        return {"portal_url": portal.url}
    except stripe.error.StripeError as e:
        raise HTTPException(500, str(e))

# ─── STRIPE WEBHOOK ───────────────────────────────────────────────────────────
@app.post("/api/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    secret  = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, secret)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid signature")

    etype = event["type"]
    obj   = event["data"]["object"]
    logger.info(f"Stripe event: {etype}")

    if etype == "checkout.session.completed":
        email = obj.get("metadata", {}).get("email", "")
        plan  = obj.get("metadata", {}).get("plan", "starter")
        if email:
            user = get_user(email)
            user["subscription"]  = plan
            user["monthly_tries"] = 0
            user["month_start"]   = time.time()
            logger.info(f"✅ Subscribed: {email} → {plan}")

    elif etype in ("customer.subscription.deleted", "invoice.payment_failed"):
        cid = obj.get("customer")
        for u in users_db.values():
            if u.get("stripe_customer_id") == cid:
                u["subscription"] = "free"
                logger.info(f"Downgraded customer {cid} to free")
                break

    return {"received": True}

# ─── TRY-ON ENDPOINT ──────────────────────────────────────────────────────────
@app.post("/api/tryon")
async def try_on(request_data: TryOnRequest, request: Request):
    start = time.time()

    if not os.getenv("REPLICATE_API_TOKEN"):
        raise HTTPException(500, "REPLICATE_API_TOKEN not set")

    # Rate limit
    check_rate_limit(get_client_ip(request))

    # User check
    user_email = request_data.user_email.strip().lower()
    user = get_user(user_email) if user_email else None

    if user:
        allowed, reason, remaining = can_try_on(user)
        if not allowed:
            frontend_url = os.getenv("FRONTEND_URL", "")
            msg = (
                "You've used all 5 free try-ons. Upgrade to continue."
                if reason == "upgrade_required"
                else f"Monthly limit reached. Resets next month."
            )
            raise HTTPException(402, json.dumps({
                "error": reason,
                "message": msg,
                "upgrade_url": f"{frontend_url}/pricing.html",
                "tries_remaining": 0,
            }))

    try:
        _, encoded = request_data.person_image.split(",", 1)
        person_bytes = base64.b64decode(encoded)

        referer = "https://www.amazon.in/" if request_data.platform == "amazon" else "https://www.flipkart.com/"
        async with httpx.AsyncClient(timeout=15.0) as client:
            cloth_resp = await client.get(
                request_data.cloth_image_url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": referer},
                follow_redirects=True,
            )
            if cloth_resp.status_code != 200:
                raise HTTPException(400, f"Could not fetch clothing image (HTTP {cloth_resp.status_code})")
            cloth_bytes = cloth_resp.content

        output = replicate.run(
            "cuuupid/idm-vton:906425dbca90663ff5427624839572cc56ea7d380343d13e2a4c4b09d3f0c30f",
            input={
                "human_img":       person_bytes,
                "garm_img":        cloth_bytes,
                "garment_des":     "clothing item",
                "is_checked":      True,
                "is_checked_crop": False,
                "denoise_steps":   30,
                "seed":            42,
            }
        )

        if not output:
            raise HTTPException(500, "AI model returned empty result")

        result_url = str(output) if not isinstance(output, list) else str(output[0])

        # Increment correct counter
        if user:
            if user["subscription"] == "free":
                user["lifetime_tries"] = user.get("lifetime_tries", 0) + 1
            else:
                user["monthly_tries"] = user.get("monthly_tries", 0) + 1

        # Refresh remaining after increment
        _, _, remaining_after = can_try_on(user) if user else (True, "ok", 999)

        elapsed = round(time.time() - start, 2)
        logger.info(f"✅ Try-on done in {elapsed}s")

        return {
            "result_url":      result_url,
            "processing_time": elapsed,
            "subscription":    user["subscription"] if user else "anonymous",
            "tries_remaining": remaining_after,
        }

    except HTTPException:
        raise
    except replicate.exceptions.ReplicateError as e:
        raise HTTPException(500, f"AI failed: {str(e)}")
    except httpx.TimeoutException:
        raise HTTPException(408, "Timed out fetching clothing image")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        raise HTTPException(500, "Internal server error")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
