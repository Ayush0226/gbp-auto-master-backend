import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import main

ORIGINAL_ACCOUNT = main.ensure_user_profile
ORIGINAL_BALANCE = main.get_token_balance
ORIGINAL_LOCATION = main.ensure_location_profile


@pytest.fixture(autouse=True)
def no_live_services(monkeypatch):
    # Every external dependency is replaced; tests must never contact production.
    monkeypatch.setattr(main,'supabase',MagicMock())
    monkeypatch.setattr(main,'requests',MagicMock())
    monkeypatch.setattr(main,'generate_ai_reply',MagicMock(return_value='Thank you!'))
    monkeypatch.setattr(main,'reserve_operation',MagicMock(return_value={'success':True}))
    monkeypatch.setattr(main,'finish_operation',MagicMock())
    monkeypatch.setattr(main,'ensure_location_profile',AsyncMock(return_value={'account_id':'accounts/1','plan_type':'yearly'}))
    monkeypatch.setattr(main,'get_token_balance',AsyncMock(return_value=100))


def run_reply():
    return asyncio.run(main.generate_and_publish('owner','locations/123','google-token',{'name':'accounts/1/locations/123/reviews/abc'},{}))


def test_generation_failure_refunds(monkeypatch):
    main.generate_ai_reply.side_effect=RuntimeError('AI unavailable')
    with pytest.raises(RuntimeError): run_reply()
    main.finish_operation.assert_called_once_with('reply:accounts/1/locations/123/reviews/abc',False)
    main.requests.put.assert_not_called()


def test_google_rejection_refunds():
    main.requests.put.return_value.ok=False
    with pytest.raises(HTTPException): run_reply()
    main.finish_operation.assert_called_once_with('reply:accounts/1/locations/123/reviews/abc',False)


def test_ambiguous_google_delivery_retains_reservation():
    main.requests.put.side_effect=TimeoutError()
    with pytest.raises(HTTPException): run_reply()
    main.finish_operation.assert_not_called()


def test_successful_reply_finalizes_charge():
    main.requests.put.return_value.ok=True
    assert run_reply()['status']=='published'
    main.finish_operation.assert_called_once_with('reply:accounts/1/locations/123/reviews/abc',True)


def test_duplicate_reply_does_not_generate_or_publish():
    main.reserve_operation.side_effect=HTTPException(409,'Already reserved')
    with pytest.raises(HTTPException): run_reply()
    main.generate_ai_reply.assert_not_called()
    main.requests.put.assert_not_called()


def test_promo_models_are_distinct():
    req=main.PromoCodeRequest(user_id='owner',location_id='123',promo_code='CODE')
    assert req.location_id=='locations/123'
    assert req.promo_code=='CODE'
    assert main.ValidatePromoRequest(user_id='owner',code='CODE').code=='CODE'


def test_empty_competitor_results_refund(monkeypatch):
    monkeypatch.setattr(main, 'get_offline_access_token', MagicMock(return_value='token'))
    monkeypatch.setattr(main, 'run_local_rank_scan', MagicMock(side_effect=RuntimeError('No local results')))
    main.supabase.auth.admin.get_user_by_id.return_value = SimpleNamespace(
        user=SimpleNamespace(user_metadata={'google_refresh_token':'refresh'})
    )
    req=main.RankReportRequest(user_id='owner',location_id='123',keyword='plumber',request_id='scan-12345678')
    with pytest.raises(HTTPException): asyncio.run(main.generate_rank_report(req))
    assert main.finish_operation.call_args.args[1] is False


def test_batch_size_is_bounded():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        main.BatchReplyRequest(user_id='owner',location_id='123',account_id='accounts/1',access_token='token',count=-1)


def test_account_rpc_result_is_awaited_before_reading_data():
    main.supabase.rpc.return_value.execute.return_value.data={'tokens_balance':200,'id':'owner'}
    assert asyncio.run(ORIGINAL_ACCOUNT('owner'))['tokens_balance']==200


def test_profiles_share_the_same_account_balance(monkeypatch):
    monkeypatch.setattr(main,'ensure_user_profile',AsyncMock(return_value={'tokens_balance':73}))
    assert asyncio.run(ORIGINAL_BALANCE('locations/1','owner'))==73
    assert asyncio.run(ORIGINAL_BALANCE('locations/2','owner'))==73


def test_location_lookup_awaits_database_result():
    main.supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data=[{'plan_type':'free'}]
    assert asyncio.run(ORIGINAL_LOCATION('123','owner'))['plan_type']=='free'


def test_topup_and_balance_do_not_require_a_business_profile():
    assert main.TopUpRequest(user_id='owner').location_id is None
    assert main.TokenBalanceRequest(user_id='owner').location_id is None


def test_campaign_payload_maps_offer_fields_for_google():
    payload = main._campaign_google_payload({
        'topic_type': 'OFFER',
        'language_code': 'en',
        'summary': 'Save this week',
        'call_to_action': {'action_type': 'LEARN_MORE', 'url': 'https://example.com'},
        'event_details': {
            'title': 'Autumn offer',
            'start_time': '2030-10-01T10:00:00+05:30',
            'end_time': '2030-10-07T18:00:00+05:30',
        },
        'offer_details': {'coupon_code': 'SAVE20', 'terms_conditions': 'One per customer'},
    }, {'media_kind': 'photo', 'public_url': 'https://example.com/photo.jpg'})
    assert payload['topicType'] == 'OFFER'
    assert payload['event']['schedule']['startDate'] == {'year': 2030, 'month': 10, 'day': 1}
    assert payload['offer']['couponCode'] == 'SAVE20'
    assert payload['media'][0]['mediaFormat'] == 'PHOTO'


def test_campaign_payload_feature_gates_video_posts():
    with pytest.raises(ValueError, match='Video attachments'):
        main._campaign_google_payload(
            {'topic_type': 'STANDARD', 'summary': 'Video'},
            {'media_kind': 'video', 'public_url': 'https://example.com/video.mp4'},
        )


def test_all_private_routes_reject_anonymous_requests(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main.app.state,'db',main.supabase)
    client=TestClient(main.app)
    for route in main.app.routes:
        path=getattr(route,'path',None)
        if not path:
            continue
        if not path.startswith('/api/') or path in ('/api/health','/api/payment/key','/api/rank/report.pdf') or path.startswith(('/api/cron/','/api/webhooks/')):
            continue
        method='POST' if 'POST' in route.methods else 'GET'
        response=client.request(method,path,json={})
        assert response.status_code==401,(path,response.status_code)
    for method,path in (
        ('GET','/api/platform/overview'),
        ('GET','/api/platform/accounts'),
        ('GET','/api/platform/automation-rules'),
        ('GET','/api/platform/campaigns'),
        ('GET','/api/platform/media'),
        ('GET','/api/platform/audit'),
        ('PUT','/api/platform/preferences'),
        ('PUT','/api/platform/automation-rules'),
        ('POST','/api/platform/campaigns'),
    ):
        response=client.request(method,path,json={})
        assert response.status_code==401,(path,response.status_code)


def test_authenticated_profile_route_reads_account(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main.app.state,'db',main.supabase)
    main.supabase.auth.get_user.return_value=SimpleNamespace(user=SimpleNamespace(id='owner',app_metadata={}))
    main.supabase.rpc.return_value.execute.return_value.data={'id':'owner','tokens_balance':200}
    response=TestClient(main.app).post('/api/user/profile',headers={'Authorization':'Bearer test-session'},json={'user_id':'owner'})
    assert response.status_code==200
    assert response.json()['tokens_balance']==200
