from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

import billing
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


@pytest.fixture
def purchase():
    db, gateway = MagicMock(), MagicMock()
    row = {'order_id':'order_1','user_id':'owner','location_id':'locations/123','kind':'subscription','product_id':'monthly','amount':50000}
    db.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data=[row]
    gateway.payment.fetch.return_value={'id':'payment_1','order_id':'order_1','amount':50000,'currency':'INR','status':'captured'}
    db.rpc.return_value.execute.return_value.data={'status':'success'}
    req=SimpleNamespace(razorpay_order_id='order_1',razorpay_payment_id='payment_1',razorpay_signature='signature',user_id='owner',location_id='locations/123',plan_id='monthly')
    return db,gateway,req


@pytest.mark.parametrize('field,value',[('plan_id','yearly'),('location_id','locations/999')])
def test_purchase_tampering_rejected(purchase,field,value):
    db,gateway,req=purchase
    setattr(req,field,value)
    with pytest.raises(HTTPException): billing.verify_order(db,gateway,req,'subscription')
    db.rpc.assert_not_called()


@pytest.mark.parametrize('field,value',[('amount',1),('currency','USD'),('status','authorized'),('order_id','other')])
def test_payment_must_be_captured_for_correct_order_and_amount(purchase,field,value):
    db,gateway,req=purchase
    gateway.payment.fetch.return_value[field]=value
    with pytest.raises(HTTPException): billing.verify_order(db,gateway,req,'subscription')
    db.rpc.assert_not_called()


def test_payment_uses_transactional_settlement(purchase):
    db,gateway,req=purchase
    assert billing.verify_order(db,gateway,req,'subscription')['status']=='success'
    db.rpc.assert_called_once_with('settle_order',{'p_order':'order_1','p_payment':'payment_1','p_user':'owner'})
    gateway.utility.verify_payment_signature.assert_called_once()


def test_promo_disabled_without_configuration(monkeypatch):
    monkeypatch.delenv('PROMO_CODE',raising=False)
    assert not billing.promo_valid('ATYAUNSUHJ')


def test_repeat_promo_rejected(monkeypatch):
    monkeypatch.setenv('PROMO_CODE','test-code')
    db,gateway=MagicMock(),MagicMock()
    db.table.return_value.select.return_value.eq.return_value.execute.return_value.data=[{'order_id':'previous'}]
    with pytest.raises(HTTPException) as error:
        billing.create_order(db,gateway,'owner','locations/123','topup','standard',{'price':500,'tokens':450},'test-code')
    assert error.value.status_code==409
    db.rpc.assert_not_called()
    gateway.order.create.assert_not_called()
