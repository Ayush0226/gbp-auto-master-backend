import pytest

from mcp_auth import protected_resource_metadata


def test_protected_resource_metadata_points_to_supabase_oauth(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://api.example.com/mcp")
    monkeypatch.setenv("MCP_DOCUMENTATION_URL", "https://example.com/privacy")

    metadata = protected_resource_metadata()

    assert metadata == {
        "resource": "https://api.example.com/mcp",
        "authorization_servers": ["https://project.supabase.co/auth/v1"],
        "scopes_supported": ["openid", "email", "profile", "offline_access"],
        "bearer_methods_supported": ["header"],
        "resource_name": "GBP Master",
        "resource_documentation": "https://example.com/privacy",
    }


@pytest.mark.parametrize(
    "setting,value",
    [
        ("SUPABASE_URL", "http://project.supabase.co"),
        ("MCP_RESOURCE_URL", "api.example.com/mcp"),
        ("MCP_DOCUMENTATION_URL", "javascript:alert(1)"),
    ],
)
def test_oauth_metadata_rejects_non_https_urls(monkeypatch, setting, value):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://api.example.com/mcp")
    monkeypatch.setenv("MCP_DOCUMENTATION_URL", "https://example.com/privacy")
    monkeypatch.setenv(setting, value)

    with pytest.raises(RuntimeError, match="absolute HTTPS URL"):
        protected_resource_metadata()
