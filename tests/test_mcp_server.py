import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from mcp_server import (
    GBPReadService,
    SupabaseTokenVerifier,
    create_gbp_mcp_server,
    mcp_transport_security,
)


def result(data):
    return SimpleNamespace(data=data)


def test_supabase_token_verifier_uses_validated_user_as_subject():
    db = MagicMock()
    db.auth.get_user.return_value = SimpleNamespace(
        user=SimpleNamespace(id="account-123", email="owner@example.com")
    )
    token = jwt.encode(
        {"sub": "account-123", "client_id": "client-1", "exp": 2_000_000_000},
        "test-only-secret-that-is-at-least-32-bytes-long",
        algorithm="HS256",
    )

    verified = asyncio.run(SupabaseTokenVerifier(db).verify_token(token))

    assert verified is not None
    assert verified.subject == "account-123"
    assert verified.client_id == "client-1"
    assert verified.scopes == ["openid", "email", "profile"]
    db.auth.get_user.assert_called_once_with(token)


def test_supabase_token_verifier_rejects_invalid_session():
    db = MagicMock()
    db.auth.get_user.side_effect = RuntimeError("expired")

    assert asyncio.run(SupabaseTokenVerifier(db).verify_token("invalid")) is None


def test_supabase_token_verifier_rejects_non_oauth_browser_session():
    db = MagicMock()
    db.auth.get_user.return_value = SimpleNamespace(
        user=SimpleNamespace(id="account-123", email="owner@example.com")
    )
    token = jwt.encode(
        {"sub": "account-123", "exp": 2_000_000_000},
        "test-only-secret-that-is-at-least-32-bytes-long",
        algorithm="HS256",
    )

    assert asyncio.run(SupabaseTokenVerifier(db).verify_token(token)) is None


def test_list_locations_is_owner_scoped_and_uses_cached_names():
    db = MagicMock()
    db.table.return_value.select.return_value.eq.return_value.execute.return_value = result(
        [
            {
                "location_id": "locations/123",
                "plan_type": "yearly",
                "subscription_end": "2030-01-01T00:00:00Z",
            }
        ]
    )
    db.auth.admin.get_user_by_id.return_value = SimpleNamespace(
        user=SimpleNamespace(
            user_metadata={
                "cached_locations": [
                    {"id": "locations/123", "name": "Ayush Cafe"}
                ]
            }
        )
    )

    response = asyncio.run(GBPReadService(db).list_locations("account-123"))

    assert response.locations[0].location_id == "locations/123"
    assert response.locations[0].name == "Ayush Cafe"
    assert response.locations[0].plan == "yearly"
    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "user_id", "account-123"
    )


def test_credit_balance_refreshes_locations_and_returns_action_costs():
    db = MagicMock()
    db.table.return_value.select.return_value.eq.return_value.execute.return_value = result(
        [{"location_id": "locations/123"}]
    )
    db.rpc.return_value.execute.side_effect = [result(None), result({"tokens_balance": 200})]

    response = asyncio.run(GBPReadService(db).get_credit_balance("account-123"))

    assert response.balance == 200
    assert response.action_costs.publish_review_reply == 2.5
    assert response.action_costs.schedule_post == 5
    assert db.rpc.call_args_list[0].args == (
        "refresh_monthly_tokens",
        {"p_location": "locations/123", "p_user": "account-123"},
    )
    assert db.rpc.call_args_list[1].args == (
        "ensure_account",
        {"p_user": "account-123"},
    )


def test_first_read_only_tools_are_registered(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://api.example.com/mcp")
    server = create_gbp_mcp_server(MagicMock())

    tools = asyncio.run(server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert set(by_name) == {"list_locations", "get_credit_balance"}
    assert by_name["list_locations"].annotations.read_only_hint is True
    assert by_name["get_credit_balance"].annotations.read_only_hint is True
    assert by_name["list_locations"].output_schema["additionalProperties"] is False


def test_mcp_endpoint_requires_bearer_token(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://api.example.com/mcp")
    server = create_gbp_mcp_server(MagicMock())
    mcp_app = server.streamable_http_app(
        transport_security=mcp_transport_security()
    )
    app = Starlette(routes=[Mount("/", app=mcp_app)])

    response = TestClient(app).post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )

    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]
