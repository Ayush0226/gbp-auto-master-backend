"""Read-only checks of stored Google grants; output contains no customer identities."""
import json
import os
from pathlib import Path
from collections import Counter

import requests
from cryptography.fernet import Fernet
from dotenv import load_dotenv

root = Path(__file__).resolve().parents[1]
load_dotenv(root / '.env.local')
db = os.environ['SUPABASE_URL'].rstrip('/')
key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
headers = {'apikey': key, 'Authorization': 'Bearer ' + key}
result = {}
response = requests.get(db + '/auth/v1/admin/users', headers=headers, params={'per_page': 100}, timeout=30)
result['auth_admin_status'] = response.status_code
users = response.json().get('users', []) if response.ok else []
result['users_first_page'] = len(users)
protected_response = requests.get(db+'/rest/v1/provider_credentials', headers=headers,
    params={'select':'user_id,ciphertext','provider':'eq.google'}, timeout=30)
protected_response.raise_for_status()
protected = {row['user_id']: row['ciphertext'] for row in protected_response.json()}
result['protected_google_grants'] = len(protected)
grants = []
for index, user in enumerate(users, 1):
    grant = {'account': index}
    refresh = (user.get('user_metadata') or {}).get('google_refresh_token')
    grant['legacy_metadata_grant'] = bool(refresh)
    if user['id'] in protected:
        refresh = Fernet(os.environ['CREDENTIAL_ENCRYPTION_KEY'].encode()).decrypt(protected[user['id']].encode()).decode()
    grant['has_google_refresh_token'] = bool(refresh)
    if refresh:
        token_response = requests.post('https://oauth2.googleapis.com/token', data={
            'client_id': os.environ['GOOGLE_CLIENT_ID'],
            'client_secret': os.environ['GOOGLE_CLIENT_SECRET'],
            'refresh_token': refresh, 'grant_type': 'refresh_token',
        }, timeout=30)
        grant['refresh_status'] = token_response.status_code
        if token_response.ok:
            token = token_response.json()['access_token']
            accounts_response = requests.get('https://mybusinessaccountmanagement.googleapis.com/v1/accounts', headers={'Authorization': 'Bearer '+token}, timeout=30)
            grant['google_accounts_status'] = accounts_response.status_code
            grant['account_count'] = len(accounts_response.json().get('accounts', []))
            account_rows = accounts_response.json().get('accounts', [])
            if account_rows:
                account_name = account_rows[0]['name']
                notice = requests.get('https://mybusinessnotifications.googleapis.com/v1/'+account_name+'/notificationSetting',headers={'Authorization':'Bearer '+token},timeout=30)
                grant['notification_settings_status'] = notice.status_code
                if notice.ok:
                    grant['pubsub_topic_configured'] = bool(notice.json().get('pubsubTopic'))
                    grant['notification_types'] = notice.json().get('notificationTypes', [])
                locations = requests.get('https://mybusinessbusinessinformation.googleapis.com/v1/'+account_name+'/locations',headers={'Authorization':'Bearer '+token},params={'readMask':'name','pageSize':100},timeout=30)
                grant['locations_status'] = locations.status_code
                grant['locations_first_page'] = len(locations.json().get('locations',[]))
        else:
            grant['error'] = token_response.json().get('error', 'unknown')
    grants.append(grant)
result['google_grants'] = grants
for table in ('token_operations', 'calendar_posts', 'account_subscriptions'):
    columns = {'token_operations':'status,updated_at', 'calendar_posts':'status,publish_at', 'account_subscriptions':'plan_type,status,expires_at'}[table]
    response = requests.get(db+'/rest/v1/'+table, headers=headers, params={'select':columns,'limit':1000}, timeout=30)
    if response.ok:
        result[table] = response.json()
auth = requests.get(db+'/auth/v1/settings', headers={'apikey':key}, timeout=30)
if auth.ok:
    result['google_provider_enabled'] = bool(auth.json().get('external', {}).get('google'))
result['serpapi'] = {'configured':bool(os.getenv('SERPAPI_KEY'))}
if os.getenv('SERPAPI_KEY'):
    response = requests.get('https://serpapi.com/account',params={'api_key':os.environ['SERPAPI_KEY']},timeout=30)
    result['serpapi']['status'] = response.status_code
    if response.ok:
        result['serpapi']['searches_left'] = response.json().get('total_searches_left')
target = root/'docs/diagnostic-google-results.json'
target.write_text(json.dumps(result, indent=2),encoding='utf-8')
print(json.dumps(result, indent=2))
