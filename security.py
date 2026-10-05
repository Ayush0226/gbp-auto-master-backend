"""Request authentication and authorization shared by every API route."""
import hmac
import os
import re

from fastapi import HTTPException, Request
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from starlette.concurrency import run_in_threadpool


def normalize_location(value: str) -> str:
    match = re.fullmatch(r"(?:accounts/[\w-]+/)?(?:locations/)?([\w-]+)", value or "")
    if not match:
        raise HTTPException(422, "Invalid location ID")
    return "locations/" + match.group(1)


async def authorize(request: Request):
    path = request.url.path
    if path in {
        "/api/health",
        "/api/payment/key",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/openai-apps-challenge",
        "/api/rank/report.pdf",
    }:
        return
    authorization = request.headers.get("authorization", "")
    token = authorization[7:] if authorization.lower().startswith('bearer ') else ''
    if path.startswith("/api/cron/"):
        secret = os.getenv("CRON_SECRET", "")
        if not secret:
            raise HTTPException(503, "Cron authentication is not configured")
        if not hmac.compare_digest(token, secret):
            raise HTTPException(401, "Invalid cron credentials")
        return
    if path == "/api/webhooks/google-reviews":
        audience = os.getenv("GOOGLE_PUBSUB_AUDIENCE", "")
        email = os.getenv("GOOGLE_PUBSUB_SERVICE_ACCOUNT", "")
        if not audience or not email:
            raise HTTPException(503, "Webhook authentication is not configured")
        try:
            claims = await run_in_threadpool(id_token.verify_oauth2_token, token, GoogleRequest(), audience)
            if claims.get("email") != email or claims.get("email_verified") is not True:
                raise ValueError("Wrong sender")
        except Exception:
            raise HTTPException(401, "Invalid webhook identity") from None
        return

    db = request.app.state.db
    if db is None:
        raise HTTPException(503, "Database is not configured")
    if not token:
        raise HTTPException(401, "Sign in to continue")
    try:
        result = await run_in_threadpool(db.auth.get_user, token)
        user = result.user
        if not user:
            raise ValueError("No user")
    except Exception:
        raise HTTPException(401, "Session expired; sign in again") from None
    request.state.user = user
    admin = (user.app_metadata or {}).get("role") == "admin"
    if (path.startswith("/api/admin/") or path == '/api/google/draft-reviews') and not admin:
        raise HTTPException(403, "Administrator access required")
    body = await request.json() if request.method == "POST" else {}
    if not isinstance(body, dict):
        raise HTTPException(422, 'Expected a JSON object')
    for key in ("user_id", "target_user_id"):
        if body.get(key) and body[key] != user.id and not admin:
            raise HTTPException(403, "You cannot access another user's account")
    location = body.get("location_id")
    review = body.get("review_name") or body.get("review_id")
    if review:
        match = re.fullmatch(r"accounts/[\w-]+/(locations/[\w-]+)/reviews/[\w-]+", review)
        if not match:
            raise HTTPException(422, "Invalid review resource")
        if location and normalize_location(location) != match.group(1):
            raise HTTPException(403, "Review does not belong to this location")
        location = match.group(1)
    if location:
        rows = await run_in_threadpool(lambda: db.table("location_profiles").select("user_id").eq("location_id", normalize_location(location)).execute())
        if not rows.data:
            raise HTTPException(409, "Reconnect Google locations before continuing")
        if rows.data[0]["user_id"] != user.id and not admin:
            raise HTTPException(403, "You do not own this location")


async def track_job(request: Request):
    """Prevent overlapping cron calls and retain a server-side run result."""
    if not request.url.path.startswith('/api/cron/'):
        yield
        return
    db = request.app.state.db
    if db is None:
        raise HTTPException(503, 'Database is not configured')
    key = request.url.path
    claimed = await run_in_threadpool(lambda: db.rpc('claim_job', {'p_key': key}).execute().data)
    if not claimed:
        raise HTTPException(409, 'This job is already running; inspect job_runs if a previous worker stopped')
    try:
        yield
    except Exception:
        await run_in_threadpool(lambda: db.table('job_runs').update({'status': 'failed', 'detail': 'Job failed; inspect server logs'}).eq('job_key', key).execute())
        raise
    else:
        await run_in_threadpool(lambda: db.table('job_runs').update({'status': 'completed', 'detail': None}).eq('job_key', key).execute())
