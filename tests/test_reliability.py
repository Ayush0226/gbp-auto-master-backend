import asyncio
import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from PIL import Image

import credential_store
import main
from image_api import normalize_photo, image_bytes
from platform_api import CampaignInput, PreferencesUpdate


class Query:
    def __init__(self, db, name):
        self.db, self.name, self.filters, self.payload = db, name, {}, None
    def select(self,*a,**k): return self
    def eq(self,k,v): self.filters[k]=v; return self
    def lte(self,*a): return self
    def limit(self,*a): return self
    def update(self,p): self.payload=p; return self
    def insert(self,p): self.payload=p; return self
    def execute(self):
        if self.name=='audit_events' and self.db.fail_audit:
            raise RuntimeError('Audit database unavailable')
        rows=[r for r in self.db.rows.get(self.name,[]) if all(r.get(k)==v for k,v in self.filters.items())]
        if self.payload and self.payload.get('status')=='published' and self.db.fail_persist:
            self.db.fail_persist=False
            raise RuntimeError('Database disconnected after acceptance')
        if self.payload:
            for row in rows: row.update(self.payload)
        return SimpleNamespace(data=rows)


class PublishingDB:
    def __init__(self):
        self.fail_persist=False; self.fail_audit=False; self.allow_claim=True
        self.rows={
            'campaign_locations':[{'campaign_id':'campaign','location_id':'locations/1','user_id':'owner','status':'scheduled'}],
            'content_campaigns':[{'id':'campaign','user_id':'owner','status':'scheduled','summary':'Hello','topic_type':'STANDARD'}],
            'review_reply_jobs':[{'id':'reply','user_id':'owner','location_id':'locations/1','review_name':'accounts/1/locations/1/reviews/a','draft_text':'Thanks','status':'scheduled'}],
        }
        self.finishes=[]
        self.auth=SimpleNamespace(admin=SimpleNamespace(get_user_by_id=lambda _:SimpleNamespace(user=SimpleNamespace(id='owner'))))
    def table(self,name): return Query(self,name)
    def rpc(self,name,params):
        def execute():
            if name in ('claim_campaign_delivery','claim_review_delivery'):
                table='campaign_locations' if name=='claim_campaign_delivery' else 'review_reply_jobs'
                if self.allow_claim: self.rows[table][0]['status']='publishing'
                return SimpleNamespace(data=self.allow_claim)
            if name=='finish_operation': self.finishes.append(params['p_success'])
            return SimpleNamespace(data={'success':True})
        return SimpleNamespace(execute=execute)


@pytest.fixture
def publishing(monkeypatch):
    db=PublishingDB()
    monkeypatch.setattr(main,'supabase',db)
    monkeypatch.setattr(main,'google_refresh_grant',lambda *_:'refresh')
    monkeypatch.setattr(main,'get_offline_access_token',lambda _:'access')
    monkeypatch.setattr(main,'registered_account',lambda _:'accounts/1')
    monkeypatch.setattr(main,'_refresh_campaign_status',MagicMock(side_effect=lambda *_:asyncio.sleep(0)))
    http=MagicMock()
    response=MagicMock(ok=True,status_code=200)
    response.json.return_value={'name':'accounts/1/locations/1/localPosts/accepted'}
    http.post.return_value=response; http.put.return_value=response
    monkeypatch.setattr(main,'requests',http)
    return db,http,response


def test_google_accepted_campaign_database_failure_stays_held(publishing):
    db,http,_=publishing; db.fail_persist=True
    result=asyncio.run(main.publish_due_campaign_deliveries())
    assert result['uncertain']==1 and result['failed']==0
    assert db.rows['campaign_locations'][0]['status']=='publishing'
    assert http.post.call_count==1


def test_lost_cancellation_claim_never_calls_google(publishing):
    db,http,_=publishing; db.allow_claim=False
    asyncio.run(main.publish_due_campaign_deliveries())
    http.post.assert_not_called()


@pytest.mark.parametrize('failure',['timeout','response','server'])
def test_uncertain_campaign_is_never_marked_retryable(publishing,failure):
    db,http,response=publishing
    if failure=='timeout': http.post.side_effect=TimeoutError()
    elif failure=='response': response.json.side_effect=ValueError('Invalid JSON')
    else: response.ok=False; response.status_code=503
    assert asyncio.run(main.publish_due_campaign_deliveries())['uncertain']==1
    assert db.rows['campaign_locations'][0]['status']=='publishing'


def test_reply_audit_failure_preserves_accepted_delivery(publishing):
    db,_,_=publishing; db.fail_audit=True
    asyncio.run(main.publish_due_review_reply_jobs())
    assert db.rows['review_reply_jobs'][0]['status']=='published'
    assert db.finishes==[True]


def test_reply_persistence_failure_keeps_guard(publishing):
    db,_,_=publishing; db.fail_persist=True
    result=asyncio.run(main.publish_due_review_reply_jobs())
    assert result['uncertain']==1
    assert db.rows['review_reply_jobs'][0]['status']=='publishing'
    assert db.finishes==[True]


def test_review_scan_traverses_older_pages(monkeypatch):
    http=MagicMock(); http.get.side_effect=[
        SimpleNamespace(ok=True,json=lambda:{'reviews':[{'name':'first'}],'nextPageToken':'page2'}),
        SimpleNamespace(ok=True,json=lambda:{'reviews':[{'name':'older'}]}),
    ]; monkeypatch.setattr(main,'requests',http)
    rows,more=asyncio.run(main.scan_google_reviews('accounts/1','locations/1','access'))
    assert [r['name'] for r in rows]==['first','older'] and more is None
    assert http.get.call_args.kwargs['params']['pageToken']=='page2'


def test_mismatched_google_identity_cannot_replace_grant(monkeypatch):
    db=MagicMock(); http=MagicMock()
    http.post.return_value=SimpleNamespace(ok=True,json=lambda:{'access_token':'access'})
    http.get.return_value=SimpleNamespace(ok=True,json=lambda:{'sub':'other-person'})
    monkeypatch.setattr(credential_store,'requests',http)
    user=SimpleNamespace(id='owner',identities=[SimpleNamespace(provider='google',identity_data={'sub':'owner-sub'})])
    with pytest.raises(PermissionError): credential_store.protect_google_grant(db,user,'refresh')
    db.table.assert_not_called(); db.auth.admin.update_user_by_id.assert_not_called()


def test_credentials_are_encrypted_and_user_scoped(monkeypatch):
    monkeypatch.setenv('CREDENTIAL_ENCRYPTION_KEY',Fernet.generate_key().decode())
    db=MagicMock()
    credential_store.save_credential(db,'owner','google','secret-refresh')
    payload=db.table.return_value.upsert.call_args.args[0]
    assert 'secret-refresh' not in str(payload)
    assert credential_store.cipher().decrypt(payload['ciphertext'].encode())==b'secret-refresh'
    assert payload['user_id']=='owner'


def test_abbreviated_admin_user_loads_immutable_identities(monkeypatch):
    db=MagicMock(); http=MagicMock()
    full=SimpleNamespace(id='owner',identities=[SimpleNamespace(provider='google',identity_data={'sub':'owner-sub'})])
    db.auth.admin.get_user_by_id.return_value=SimpleNamespace(user=full)
    http.post.return_value=SimpleNamespace(ok=True,json=lambda:{'access_token':'access'})
    http.get.return_value=SimpleNamespace(ok=True,json=lambda:{'sub':'owner-sub'})
    monkeypatch.setattr(credential_store,'requests',http)
    save=MagicMock(); monkeypatch.setattr(credential_store,'save_credential',save)
    assert credential_store.protect_google_grant(db,SimpleNamespace(id='owner',identities=[]),'refresh')=='refresh'
    db.auth.admin.get_user_by_id.assert_called_once_with('owner')
    save.assert_called_once_with(db,'owner','google','refresh',subject='owner-sub')


def test_valid_photo_is_transcoded_and_metadata_removed():
    data=io.BytesIO(); Image.new('RGB',(400,300),'green').save(data,'PNG')
    raw,w,h=normalize_photo(data.getvalue())
    assert (w,h)==(400,300)
    with Image.open(io.BytesIO(raw)) as image: assert image.format=='JPEG'


@pytest.mark.parametrize('raw',[b'<svg><script/></svg>',b'not an image'])
def test_invalid_media_is_rejected(raw):
    with pytest.raises(HTTPException): normalize_photo(raw)


def test_provider_exhaustion_is_visible():
    with pytest.raises(HTTPException) as error: image_bytes(SimpleNamespace(status_code=402))
    assert error.value.status_code==429


def test_timezone_validation():
    from pydantic import ValidationError
    with pytest.raises(ValidationError): PreferencesUpdate(timezone='Made/Up')
    with pytest.raises(ValidationError): CampaignInput(title='Test',timezone='Made/Up',location_ids=['locations/1'])


def test_completed_rank_scan_is_replayed_without_provider_call():
    from report_cache import measured_report
    db=MagicMock(); db.rpc.return_value.execute.return_value.data={'success':True,'replayed':True,'result':{'actual_rank':3}}
    async def fail_token(): raise AssertionError('A replay must not refresh or scan')
    scanner=MagicMock(side_effect=AssertionError('No new provider request'))
    result=asyncio.run(measured_report(db,'owner','locations/1','coffee','stable-123',fail_token,scanner))
    assert result=={'actual_rank':3};scanner.assert_not_called()
