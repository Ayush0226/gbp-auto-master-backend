from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from security import authorize, normalize_location


@pytest.fixture
def secured():
    db = MagicMock()
    user = SimpleNamespace(id='owner', app_metadata={})
    db.auth.get_user.return_value = SimpleNamespace(user=user)
    db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{'user_id':'owner'}]
    app = FastAPI(dependencies=[Depends(authorize)])
    app.state.db = db
    @app.post('/api/user/profile')
    @app.post('/api/admin/users')
    @app.post('/api/google/post-reply')
    @app.get('/api/cron/publish-scheduled')
    @app.post('/api/webhooks/google-reviews')
    @app.get('/.well-known/oauth-protected-resource')
    async def endpoint():
        return {'status':'success'}
    return TestClient(app), db, user


def test_anonymous_request_rejected(secured):
    client, db, _ = secured
    assert client.post('/api/user/profile', json={'user_id':'owner'}).status_code == 401
    db.auth.get_user.assert_not_called()


def test_oauth_resource_metadata_is_public(secured):
    client, db, _ = secured
    assert client.get('/.well-known/oauth-protected-resource').status_code == 200
    db.auth.get_user.assert_not_called()


def test_forged_user_rejected(secured):
    client, _, _ = secured
    assert client.post('/api/user/profile', headers={'Authorization':'Bearer session'}, json={'user_id':'victim'}).status_code == 403


def test_forged_admin_email_does_not_grant_access(secured):
    client, _, _ = secured
    assert client.post('/api/admin/users', headers={'Authorization':'Bearer session'}, json={'admin_email':'ayushsony126@gmail.com'}).status_code == 403


def test_server_owned_admin_role(secured):
    client, _, user = secured
    user.app_metadata = {'role':'admin'}
    assert client.post('/api/admin/users', headers={'Authorization':'Bearer session'}, json={}).status_code == 200


def test_location_ownership(secured):
    client, db, _ = secured
    db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{'user_id':'victim'}]
    assert client.post('/api/user/profile', headers={'Authorization':'Bearer session'}, json={'user_id':'owner','location_id':'123'}).status_code == 403


def test_review_resource_must_match_location(secured):
    client, _, _ = secured
    assert client.post('/api/google/post-reply', headers={'Authorization':'Bearer session'}, json={'location_id':'123','review_id':'accounts/1/locations/999/reviews/abc'}).status_code == 403


def test_cron_requires_secret(secured, monkeypatch):
    client, _, _ = secured
    monkeypatch.setenv('CRON_SECRET','test-secret')
    assert client.get('/api/cron/publish-scheduled').status_code == 401
    assert client.get('/api/cron/publish-scheduled',headers={'Authorization':'Bearer test-secret'}).status_code == 200


def test_webhook_requires_identity_configuration(secured, monkeypatch):
    client, _, _ = secured
    monkeypatch.delenv('GOOGLE_PUBSUB_AUDIENCE',raising=False)
    assert client.post('/api/webhooks/google-reviews',json={}).status_code == 503


@pytest.mark.parametrize('value',['123','locations/123','accounts/456/locations/123'])
def test_canonical_locations(value):
    assert normalize_location(value)=='locations/123'


@pytest.mark.parametrize('value',['../123','locations/123?x=1','https://evil.test/123',''])
def test_invalid_location(value):
    with pytest.raises(HTTPException): normalize_location(value)
