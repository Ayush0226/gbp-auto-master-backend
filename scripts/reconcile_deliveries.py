"""Inspect overdue legacy posts against Google. Never publishes or resets jobs."""
import json
from pathlib import Path

import requests
from cryptography.fernet import Fernet
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]


def inspect():
    env=dotenv_values(ROOT/'.env.local'); base=env['SUPABASE_URL'].rstrip('/')
    headers={'apikey':env['SUPABASE_SERVICE_ROLE_KEY'],'Authorization':'Bearer '+env['SUPABASE_SERVICE_ROLE_KEY']}
    def get(path,**kwargs):
        response=requests.get(base+path,headers=headers,timeout=30,**kwargs); response.raise_for_status(); return response.json()
    posts=get('/rest/v1/calendar_posts',params={'select':'*','status':'in.(scheduled,publishing,failed,needs_review)'})
    results=[]
    for post in posts:
        user=get('/auth/v1/admin/users/'+post['user_id'])
        refresh=user.get('user_metadata',{}).get('google_refresh_token')
        protected=get('/rest/v1/provider_credentials',params={'select':'ciphertext','user_id':'eq.'+post['user_id'],'provider':'eq.google'})
        if protected:
            refresh=Fernet(env['CREDENTIAL_ENCRYPTION_KEY'].encode()).decrypt(protected[0]['ciphertext'].encode()).decode()
        if not refresh:
            results.append({'post_id':post['id'],'result':'Google reconnect required'});continue
        auth=requests.post('https://oauth2.googleapis.com/token',data={'client_id':env['GOOGLE_CLIENT_ID'],
            'client_secret':env['GOOGLE_CLIENT_SECRET'],'refresh_token':refresh,'grant_type':'refresh_token'},timeout=30)
        auth.raise_for_status(); token=auth.json()['access_token']
        profile=get('/rest/v1/location_profiles',params={'select':'account_id','location_id':'eq.'+post['location_id']})[0]
        url=f"https://mybusiness.googleapis.com/v4/{profile['account_id']}/{post['location_id']}/localPosts"
        candidates=[]; page=None; more=False
        for _ in range(5):
            response=requests.get(url,headers={'Authorization':'Bearer '+token},params={'pageSize':100,**({'pageToken':page} if page else {})},timeout=30)
            response.raise_for_status(); data=response.json()
            candidates.extend({'name':row.get('name'),'create_time':row.get('createTime'),'state':row.get('state')}
                for row in data.get('localPosts',[]) if row.get('summary','').strip()==post.get('caption','').strip())
            page=data.get('nextPageToken')
            if not page: break
        more=bool(page)
        results.append({'post_id':post['id'],'due':post.get('publish_at'),'status':post['status'],
            'matching_google_candidates':candidates,'scan_truncated':more,
            'decision':'Owner review required before rescheduling; no public write or status reset performed'})
    path=ROOT/'docs'/'delivery-reconciliation-2026-11-10.json'
    path.write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results,indent=2))


if __name__=='__main__': inspect()
