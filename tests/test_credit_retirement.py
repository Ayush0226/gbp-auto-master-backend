import asyncio
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import main


def test_retired_sales_and_balance_routes_are_not_exposed():
    paths = {getattr(route, 'path', '') for route in main.app.routes}
    assert not any(path.startswith('/api/tokens/') for path in paths)
    assert '/api/payment/create-topup-order' not in paths
    assert '/api/payment/verify-topup' not in paths


def test_delivery_claim_has_no_balance_or_price_dependency(monkeypatch):
    db = MagicMock()
    db.rpc.return_value.execute.return_value.data = {'success': True}
    monkeypatch.setattr(main, 'supabase', db)
    assert main.reserve_operation('123', 'owner', 'reply-1') == {'success': True}
    db.rpc.assert_called_once_with('claim_operation', {
        'p_operation': 'reply-1', 'p_location': 'locations/123', 'p_user': 'owner',
    })


def test_readiness_fails_when_database_is_unavailable(monkeypatch):
    monkeypatch.setattr(main.app.state, 'db', None)
    response = TestClient(main.app).get('/api/ready')
    assert response.status_code == 503


def test_account_profile_does_not_expose_archived_balances(monkeypatch):
    db = MagicMock()
    db.rpc.return_value.execute.return_value.data = {'id': 'owner', 'tokens_balance': 200, 'last_daily_claim': '2026-01-01'}
    monkeypatch.setattr(main, 'supabase', db)
    assert asyncio.run(main.ensure_user_profile('owner')) == {'id': 'owner'}
