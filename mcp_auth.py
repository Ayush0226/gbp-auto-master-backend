"""OAuth discovery helpers for the GBP Master MCP resource server."""
import os
from urllib.parse import urlparse


OAUTH_SCOPES = ("openid", "email", "profile")
DEFAULT_MCP_RESOURCE_URL = "https://gbp-auto-master-backend-us.onrender.com/mcp"


def _https_url(value: str, setting: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(f"{setting} must be an absolute HTTPS URL")
    return value.rstrip("/")


def protected_resource_metadata() -> dict:
    """Return RFC 9728 metadata used by MCP clients to start account linking."""
    supabase_url = _https_url(os.getenv("SUPABASE_URL", ""), "SUPABASE_URL")
    resource = _https_url(
        os.getenv("MCP_RESOURCE_URL", DEFAULT_MCP_RESOURCE_URL),
        "MCP_RESOURCE_URL",
    )
    documentation = _https_url(
        os.getenv("MCP_DOCUMENTATION_URL", "https://gbpautomaster.in/privacy"),
        "MCP_DOCUMENTATION_URL",
    )
    return {
        "resource": resource,
        "authorization_servers": [f"{supabase_url}/auth/v1"],
        "scopes_supported": list(OAUTH_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "GBP Master",
        "resource_documentation": documentation,
    }
