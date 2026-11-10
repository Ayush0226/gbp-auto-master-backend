"""Encrypted credentials accessible only through the backend service role."""
import os
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from http_client import requests


def cipher():
    key = os.getenv('CREDENTIAL_ENCRYPTION_KEY', '')
    if not key:
        raise RuntimeError('Protected credential storage is not configured')
    return Fernet(key.encode())


def save_credential(db, user_id, provider, secret, subject=None, expires_at=None):
    db.table('provider_credentials').upsert({
        'user_id': user_id, 'provider': provider,
        'ciphertext': cipher().encrypt(secret.encode()).decode(),
        'subject': subject, 'expires_at': expires_at,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }, on_conflict='user_id,provider').execute()


def read_credential(db, user_id, provider):
    rows = db.table('provider_credentials').select('ciphertext,expires_at').eq('user_id', user_id).eq('provider', provider).limit(1).execute().data or []
    if not rows:
        return None
    row = rows[0]
    if row.get('expires_at') and datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00')) <= datetime.now(timezone.utc):
        raise PermissionError('Provider authorization expired; reconnect your account')
    try:
        return cipher().decrypt(row['ciphertext'].encode()).decode()
    except InvalidToken:
        raise RuntimeError('Credential cannot be decrypted; check the server encryption key') from None


def protect_google_grant(db, user, refresh_token):
    """Validate against immutable Auth identities before adopting a grant."""
    response = requests.post('https://oauth2.googleapis.com/token', data={
        'client_id': os.getenv('GOOGLE_CLIENT_ID'), 'client_secret': os.getenv('GOOGLE_CLIENT_SECRET'),
        'refresh_token': refresh_token, 'grant_type': 'refresh_token',
    })
    if not response.ok:
        raise PermissionError('Google authorization expired; reconnect Google')
    access = response.json().get('access_token')
    identity = requests.get('https://openidconnect.googleapis.com/v1/userinfo', headers={'Authorization': f'Bearer {access}'})
    if not identity.ok:
        raise PermissionError('Could not verify the connected Google identity')
    subject = identity.json().get('sub')
    subjects = {
        (getattr(item, 'identity_data', None) or {}).get('sub')
        for item in (getattr(user, 'identities', None) or [])
        if getattr(item, 'provider', None) == 'google'
    }
    if not subject or subject not in subjects:
        raise PermissionError('Connect the Google identity used to sign in; existing ownership was preserved')
    save_credential(db, user.id, 'google', refresh_token, subject=subject)
    db.table('google_connections').upsert({
        'user_id': user.id, 'google_subject': subject,
        'email': identity.json().get('email'), 'status': 'connected',
        'token_secret_ref': 'provider_credentials:google',
        'scopes': str(response.json().get('scope', '')).split(),
        'last_synced_at': datetime.now(timezone.utc).isoformat(),
    }, on_conflict='user_id,google_subject').execute()
    # Null removes the usable grant from editable/browsable account metadata.
    # Other metadata, roles, sessions and permissions are preserved.
    db.auth.admin.update_user_by_id(user.id, {'user_metadata': {'google_refresh_token': None}})
    return refresh_token


def google_refresh_grant(db, user):
    protected = read_credential(db, user.id, 'google')
    if protected:
        return protected
    legacy = (getattr(user, 'user_metadata', None) or {}).get('google_refresh_token')
    if legacy:
        return protect_google_grant(db, user, legacy)
    return None
