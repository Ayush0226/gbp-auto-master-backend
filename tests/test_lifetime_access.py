import asyncio
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import billing
import main
from entitlements import resolve_subscription
from mcp_server import GBPReadService


def lifetime_row():
    return {'plan_type': 'yearly', 'status': 'active', 'expires_at': None,
            'is_lifetime': True, 'access_scope': 'current_features_v1'}


def test_lifetime_access_stays_active_far_beyond_a_year():
    result = resolve_subscription(lifetime_row(), now=datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert result['plan_type'] == 'yearly'
    assert result['is_lifetime'] and result['unlimited_access']
    assert result['expires_at'] is None
    assert result['max_seo_keywords'] is None
    assert result['max_locations'] is None
    assert result['competitor_access'] is True


@pytest.mark.parametrize('row', [
    {**lifetime_row(), 'status': 'cancelled'},
    {'plan_type': 'yearly', 'status': 'active', 'expires_at': '2020-01-01T00:00:00Z'},
    {'plan_type': 'yearly', 'status': 'active', 'expires_at': None},
])
def test_expired_or_cancelled_access_does_not_unlock_lifetime(row):
    result = resolve_subscription(row)
    assert result['plan_type'] == 'free'
    assert not result['unlimited_access']
    assert result['max_seo_keywords'] == 2


def test_code_is_normalized_hashed_and_redeemed_server_side():
    db = MagicMock()
    db.rpc.return_value.execute.return_value.data = {'success': True, 'subscription': lifetime_row()}
    assert billing.redeem_access_code(db, 'owner', '  atyaunsuhj  ')['success']
    db.rpc.assert_called_once_with('redeem_lifetime_access', {
        'p_user': 'owner', 'p_code_hash': hashlib.sha256(b'ATYAUNSUHJ').hexdigest(),
    })


def test_invalid_code_cannot_grant_access():
    db = MagicMock()
    db.rpc.return_value.execute.return_value.data = {'success': False}
    with pytest.raises(HTTPException) as error:
        billing.redeem_access_code(db, 'owner', 'wrong-code')
    assert error.value.status_code == 400


@pytest.fixture
def signed_client(monkeypatch):
    db = MagicMock()
    db.auth.get_user.return_value = SimpleNamespace(user=SimpleNamespace(id='owner', app_metadata={}))
    db.rpc.return_value.execute.return_value.data = {'success': True, 'subscription': lifetime_row()}
    monkeypatch.setattr(main.app.state, 'db', db)
    return TestClient(main.app), db, {'Authorization': 'Bearer session'}


def test_redemption_uses_authenticated_identity_and_rejects_other_user(signed_client):
    client, db, headers = signed_client
    response = client.post('/api/billing/redeem-access-code', headers=headers, json={'code': 'ATYAUNSUHJ'})
    assert response.status_code == 200
    assert db.rpc.call_args.args[1]['p_user'] == 'owner'
    db.rpc.reset_mock()
    response = client.post('/api/billing/redeem-access-code', headers=headers, json={'code': 'ATYAUNSUHJ', 'user_id': 'victim'})
    assert response.status_code == 403
    db.rpc.assert_not_called()


def test_anonymous_redemption_is_rejected(signed_client):
    client, db, _ = signed_client
    assert client.post('/api/billing/redeem-access-code', json={'code': 'ATYAUNSUHJ'}).status_code == 401
    db.rpc.assert_not_called()


def test_payment_routes_are_paused_without_contacting_a_gateway(signed_client):
    client, db, headers = signed_client
    for path in ['/api/payment/create-order', '/api/payment/verify']:
        assert client.post(path, headers=headers, json={}).status_code == 503
    response = client.get('/api/payment/key')
    assert response.status_code == 503
    assert 'key' not in response.json()
    db.rpc.assert_not_called()


def test_lifetime_user_can_save_more_than_yearly_keyword_limit(monkeypatch):
    monkeypatch.setattr(main, 'ensure_location_profile', AsyncMock(return_value={**lifetime_row(), 'unlimited_access': True}))
    db = MagicMock()
    db.auth.admin.get_user_by_id.return_value.user.user_metadata = {}
    monkeypatch.setattr(main, 'supabase', db)
    keywords = [f'keyword-{number}' for number in range(30)]
    asyncio.run(main.save_ai_settings(main.SaveAISettingsRequest(user_id='owner', location_id='123', settings={'active_keywords': keywords})))
    payload = db.auth.admin.update_user_by_id.call_args.args[1]
    assert payload['user_metadata']['ai_settings']['locations/123']['active_keywords'] == keywords


def test_mcp_location_keeps_lifetime_plan_with_no_expiry():
    db = MagicMock()
    db.table.return_value.select.return_value.eq.return_value.execute.side_effect = [
        SimpleNamespace(data=[{'location_id': 'locations/123', 'account_id': 'accounts/1', 'plan_type': 'free'}]),
        SimpleNamespace(data=[lifetime_row()]),
    ]
    db.auth.admin.get_user_by_id.return_value.user.user_metadata = {}
    location = asyncio.run(GBPReadService(db).list_locations('owner')).locations[0]
    assert location.plan == 'yearly'
    assert location.is_lifetime and location.unlimited_access
    assert location.subscription_expires_at is None


def test_onboarding_derives_identity_and_keeps_unlimited_keywords(signed_client, monkeypatch):
    client, db, headers = signed_client
    monkeypatch.setattr(main, 'supabase', db)
    monkeypatch.setattr(main, 'ensure_user_profile', AsyncMock(return_value={'plan_type': 'yearly', 'unlimited_access': True}))
    keywords = [f'keyword-{index}' for index in range(30)]
    response = client.post('/api/user/onboarding', headers=headers, json={'reply_length': '50-90', 'seo_keywords': keywords})
    assert response.status_code == 200
    assert response.json()['keywords_saved'] == 30
    assert response.json()['max_allowed'] is None
    db.table.return_value.update.return_value.eq.assert_called_with('id', 'owner')
