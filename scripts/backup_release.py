"""Encrypted application-data snapshot. Never exports secrets to stdout."""
import json
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography.fernet import Fernet
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]


def backup():
    env = dotenv_values(ROOT / '.env.local')
    key = env.get('CREDENTIAL_ENCRYPTION_KEY')
    if not key:
        key = Fernet.generate_key().decode()
        set_key(ROOT / '.env.local', 'CREDENTIAL_ENCRYPTION_KEY', key)
    cipher = Fernet(key.encode())
    headers = {'apikey':env['SUPABASE_SERVICE_ROLE_KEY'], 'Authorization':'Bearer '+env['SUPABASE_SERVICE_ROLE_KEY']}
    base = env['SUPABASE_URL'].rstrip('/')
    schema = requests.get(base+'/rest/v1/',headers=headers,timeout=30)
    schema.raise_for_status()
    data = {'created_at':datetime.now(timezone.utc).isoformat(),'project_url':base,'tables':{}}
    for table in schema.json().get('definitions',{}):
        rows = []
        for offset in range(0,100000,500):
            response = requests.get(base+'/rest/v1/'+table,headers=headers,params={'select':'*','offset':offset,'limit':500},timeout=30)
            response.raise_for_status()
            chunk = response.json()
            rows.extend(chunk)
            if len(chunk)<500:
                break
        else:
            raise RuntimeError('Backup limit reached; no snapshot was written')
        data['tables'][table] = rows
    response = requests.get(base+'/auth/v1/admin/users',headers=headers,params={'page':1,'per_page':1000},timeout=30)
    response.raise_for_status()
    data['auth_users'] = response.json()
    if len(data['auth_users'].get('users',[]))>=1000:
        raise RuntimeError('Auth backup needs pagination')
    directory = Path.home()/'.codex'/'backups'/'gbp-master'
    directory.mkdir(parents=True,exist_ok=True)
    path = directory/('release-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json.fernet')
    path.write_bytes(cipher.encrypt(json.dumps(data).encode()))
    # Verify the full encrypted round trip before reporting success.
    assert json.loads(cipher.decrypt(path.read_bytes())) == data
    print(json.dumps({'backup':str(path),'tables':len(data['tables']),'rows':sum(map(len,data['tables'].values())),
        'auth_users':len(data['auth_users'].get('users',[])), 'encrypted_round_trip':'verified'}))


if __name__=='__main__':
    backup()
