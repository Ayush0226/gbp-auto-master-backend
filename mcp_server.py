"""Authenticated MCP server for GBP Auto Master."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from mcp_auth import DEFAULT_MCP_RESOURCE_URL, OAUTH_SCOPES


SERVER_INSTRUCTIONS = (
    "Use GBP Master to inspect and manage the authenticated user's Google Business "
    "Profile locations. Resolve a location with list_locations before using a "
    "location-scoped tool. Never ask for or return Google access tokens, refresh "
    "tokens, Supabase user IDs, or Google account IDs."
)


class LocationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_id: str
    name: str
    plan: str
    subscription_expires_at: datetime | None


class LocationList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locations: list[LocationSummary]


class ActionCosts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publish_review_reply: float = 2.5
    schedule_post: float = 5.0


class CreditBalance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    balance: float
    action_costs: ActionCosts


def _scopes(claims: dict[str, Any]) -> list[str]:
    raw = claims.get("scope") or claims.get("scopes")
    if isinstance(raw, str):
        scopes = raw.split()
    elif isinstance(raw, list):
        scopes = [scope for scope in raw if isinstance(scope, str)]
    else:
        scopes = []
    # Supabase validates the account token. Some Supabase sessions do not repeat
    # the OAuth grant scopes in the JWT, so use the server's supported identity
    # scopes after successful validation.
    return scopes or list(OAUTH_SCOPES)


class SupabaseTokenVerifier(TokenVerifier):
    """Validate MCP bearer tokens with Supabase Auth and expose the account id."""

    def __init__(self, db: Any):
        self.db = db

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or self.db is None:
            return None
        try:
            result = await run_in_threadpool(self.db.auth.get_user, token)
            user = result.user
            if not user:
                return None
            claims = jwt.decode(
                token,
                options={"verify_signature": False, "verify_exp": False, "verify_aud": False},
            )
        except Exception:
            return None

        client_id = claims.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            # Supabase OAuth Server access tokens include client_id. Reject ordinary
            # browser sessions so only a user-approved OAuth application can call MCP.
            return None
        expires_at = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=_scopes(claims),
            expires_at=int(expires_at) if isinstance(expires_at, (int, float)) else None,
            subject=str(user.id),
            claims={"email": getattr(user, "email", None)},
        )


class GBPReadService:
    """Account-scoped reads used by MCP tools."""

    def __init__(self, db: Any):
        self.db = db

    async def list_locations(self, user_id: str) -> LocationList:
        rows = (
            await run_in_threadpool(
                lambda: self.db.table("location_profiles")
                .select("location_id,plan_type,subscription_end")
                .eq("user_id", user_id)
                .execute()
            )
        ).data or []

        auth_user = await run_in_threadpool(self.db.auth.admin.get_user_by_id, user_id)
        metadata = getattr(auth_user.user, "user_metadata", None) or {}
        cached = metadata.get("cached_locations") or []
        names = {
            item.get("id"): item.get("name")
            for item in cached
            if isinstance(item, dict) and item.get("id")
        }
        now = datetime.now(timezone.utc)
        locations: list[LocationSummary] = []
        for row in rows:
            expires_at = _parse_datetime(row.get("subscription_end"))
            plan = row.get("plan_type") or "free"
            if plan != "free" and (expires_at is None or expires_at <= now):
                plan = "free"
            location_id = row["location_id"]
            locations.append(
                LocationSummary(
                    location_id=location_id,
                    name=names.get(location_id) or "Business",
                    plan=plan,
                    subscription_expires_at=expires_at,
                )
            )
        return LocationList(locations=locations)

    async def get_credit_balance(self, user_id: str) -> CreditBalance:
        location_rows = (
            await run_in_threadpool(
                lambda: self.db.table("location_profiles")
                .select("location_id")
                .eq("user_id", user_id)
                .execute()
            )
        ).data or []
        for row in location_rows:
            await run_in_threadpool(
                lambda location_id=row["location_id"]: self.db.rpc(
                    "refresh_monthly_tokens",
                    {"p_location": location_id, "p_user": user_id},
                ).execute()
            )
        account = (
            await run_in_threadpool(
                lambda: self.db.rpc("ensure_account", {"p_user": user_id}).execute()
            )
        ).data or {}
        return CreditBalance(
            balance=max(0.0, float(account.get("tokens_balance") or 0)),
            action_costs=ActionCosts(),
        )


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _subject() -> str:
    access_token = get_access_token()
    if access_token is None or not access_token.subject:
        raise PermissionError("Authentication required")
    return access_token.subject


def create_gbp_mcp_server(db: Any) -> MCPServer:
    # Startup validation in main.py rejects missing production settings. These
    # import-safe defaults let tooling and unit tests inspect the ASGI app without
    # requiring deployment secrets.
    issuer_url = os.getenv("SUPABASE_URL", "https://project.supabase.co").rstrip("/") + "/auth/v1"
    resource_url = os.getenv("MCP_RESOURCE_URL", DEFAULT_MCP_RESOURCE_URL).rstrip("/")
    documentation_url = os.getenv(
        "MCP_DOCUMENTATION_URL", "https://gbpautomaster.in/privacy"
    )
    read_service = GBPReadService(db)
    server = MCPServer(
        name="gbp-master",
        title="GBP Master",
        description="Manage Google Business Profile reviews, credits, and scheduled content.",
        instructions=SERVER_INSTRUCTIONS,
        website_url="https://gbpautomaster.in",
        version="1.0.0",
        token_verifier=SupabaseTokenVerifier(db),
        auth=AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=resource_url,
            service_documentation_url=documentation_url,
            required_scopes=list(OAUTH_SCOPES),
            validate_token_resource=False,
        ),
    )

    @server.tool(
        name="list_locations",
        title="List business locations",
        description=(
            "List Google Business Profile locations owned by the authenticated GBP "
            "Master account. Use this before a location-scoped tool."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def list_locations() -> LocationList:
        return await read_service.list_locations(_subject())

    @server.tool(
        name="get_credit_balance",
        title="Get credit balance",
        description=(
            "Return the authenticated account's shared GBP Master credit balance and "
            "the credit cost of supported write actions."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def get_credit_balance() -> CreditBalance:
        return await read_service.get_credit_balance(_subject())

    return server


def mcp_transport_security() -> TransportSecuritySettings:
    resource_url = os.getenv("MCP_RESOURCE_URL", DEFAULT_MCP_RESOURCE_URL)
    resource_host = urlparse(resource_url).netloc
    allowed_origins = [
        origin.strip()
        for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            resource_host,
            f"{resource_host}:*",
            "localhost:*",
            "127.0.0.1:*",
            "testserver",
        ],
        allowed_origins=allowed_origins
        + ["http://localhost:*", "http://127.0.0.1:*"],
    )
