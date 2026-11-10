"""Probe unauthenticated access only; never sends cron credentials."""
import json
from pathlib import Path
import requests
from dotenv import dotenv_values

root = Path(__file__).resolve().parents[1]
env = dotenv_values(root/'.env.local')
base = 'https://gbp-auto-master-backend-us.onrender.com'
result = {}
for method, path in [('GET','/api/health'),('GET','/api/cron/reply-reviews'),('GET','/api/cron/publish-scheduled'),('POST','/api/webhooks/google-reviews'),('GET','/api/ready')]:
    try:
        response = requests.request(method,base+path,json={} if method=='POST' else None,timeout=30)
        entry = {'status':response.status_code}
        try:
            entry['detail'] = response.json().get('detail')
        except ValueError:
            entry['content_type'] = response.headers.get('content-type')
        result[path] = entry
    except requests.RequestException as error:
        result[path] = {'error':type(error).__name__}
ui_env = dotenv_values(root.parent/'gbp-auto-master-ui/.env.production')
anonymous_key = ui_env['VITE_SUPABASE_ANON_KEY']
anonymous_headers = {'apikey':anonymous_key, 'Authorization':'Bearer '+anonymous_key}
response = requests.get(env['SUPABASE_URL']+'/rest/v1/',headers=anonymous_headers,timeout=30)
result['anonymous_schema'] = {'status':response.status_code,'tables':list(response.json().get('definitions',{})),'rpcs':[p for p in response.json().get('paths',{}) if p.startswith('/rpc/')]}
for table in ['user_profiles','location_profiles','account_token_ledger','review_reply_jobs','billing_orders','operation_runs']:
    response = requests.get(env['SUPABASE_URL']+'/rest/v1/'+table+'?select=*&limit=1',headers=anonymous_headers,timeout=30)
    result['anonymous_'+table] = {'status':response.status_code}
    if response.ok and isinstance(response.json(),list):
        result['anonymous_'+table]['rows_visible_up_to_one'] = len(response.json())
(root/'docs/diagnostic-access-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
