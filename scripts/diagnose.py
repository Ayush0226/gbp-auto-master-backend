"""Read-only integration diagnostics. Never prints credentials or customer content."""
import concurrent.futures
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env.local')


def scan():
    base = os.getenv('PUBLIC_API_URL', 'https://gbp-auto-master-backend-us.onrender.com').rstrip('/')
    db = os.getenv('SUPABASE_URL', '').rstrip('/')
    key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
    headers = {'apikey': key, 'Authorization': f'Bearer {key}'}
    checks = {
        'backend_health': (base + '/api/health', {}),
        'mcp_resource_discovery': (base + '/.well-known/oauth-protected-resource', {}),
        'mcp_unauthenticated': (base + '/mcp', {'headers': {'Accept': 'text/event-stream'}}),
        'frontend': ('https://gbpautomaster.in', {}),
        'supabase_auth_health': (db + '/auth/v1/health', {'headers': headers}),
        'oauth_discovery': (db + '/auth/v1/.well-known/oauth-authorization-server', {}),
        'supabase_schema': (db + '/rest/v1/', {'headers': headers}),
        'groq_models': ('https://api.groq.com/openai/v1/models', {'headers': {'Authorization': 'Bearer ' + os.getenv('GROQ_API_KEY', '')}}),
    }
    tables = ['user_profiles', 'location_profiles', 'account_subscriptions', 'gbp_accounts', 'google_connections', 'review_automation_rules', 'content_campaigns', 'campaign_locations', 'review_reply_jobs', 'inbound_events', 'audit_events', 'job_runs', 'billing_orders', 'calendar_posts', 'media_assets', 'operation_runs']
    for table in tables:
        checks['table_' + table] = (db + '/rest/v1/' + table + '?select=*&limit=0', {'headers': {**headers, 'Prefer': 'count=exact'}})
    checks['cron_runs'] = (db + '/rest/v1/job_runs?select=job_key,status,updated_at,detail', {'headers': headers})
    for table in ['calendar_posts', 'campaign_locations', 'review_reply_jobs', 'inbound_events']:
        checks['states_' + table] = (db + '/rest/v1/' + table + '?select=status&limit=1000', {'headers': headers})

    def one(item):
        name, (url, kwargs) = item
        try:
            response = requests.get(url, timeout=35, **kwargs)
            result = {'status': response.status_code}
            if response.headers.get('content-range'):
                result['count_range'] = response.headers['content-range']
            if name in ('backend_health', 'mcp_resource_discovery', 'oauth_discovery', 'cron_runs'):
                try:
                    result['data'] = response.json()
                except ValueError:
                    result['content_type'] = response.headers.get('content-type')
            if name.startswith('states_') and response.ok:
                states = {}
                for row in response.json():
                    state = row.get('status', 'unknown')
                    states[state] = states.get(state, 0) + 1
                result['states_first_1000'] = states
            if name == 'supabase_schema' and response.ok:
                payload = response.json()
                result['tables'] = sorted(payload.get('definitions', {}))
                result['rpcs'] = sorted(p for p in payload.get('paths', {}) if p.startswith('/rpc/'))
            if name.startswith('table_') and not response.ok:
                result['error_code'] = response.json().get('code')
            if name == 'mcp_unauthenticated':
                result['www_authenticate'] = response.headers.get('www-authenticate')
            return name, result
        except Exception as error:
            return name, {'error': type(error).__name__}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, checks.items()))


if __name__ == '__main__':
    result = scan()
    target = ROOT / 'docs' / 'diagnostic-live-results.json'
    target.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
