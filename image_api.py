"""Official provider integrations; images enter the existing owned media flow."""
import base64
import hashlib
import io
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from credential_store import cipher, read_credential, save_credential
from http_client import requests

router = APIRouter(prefix='/api/images', tags=['image-studio'])
MODEL = '@cf/black-forest-labs/flux-2-klein-4b'
MAX_BYTES = 8 * 1024 * 1024


def provider_status(db, user_id):
    connected = False
    try:
        connected = bool(read_credential(db, user_id, 'pollinations'))
    except PermissionError:
        pass
    return {
        'cloudflare_available': bool(os.getenv('CLOUDFLARE_ACCOUNT_ID') and os.getenv('CLOUDFLARE_API_TOKEN')),
        'pollinations_available': bool(os.getenv('POLLINATIONS_APP_KEY') and os.getenv('POLLINATIONS_REDIRECT_URI')),
        'pollinations_connected': connected,
    }


@router.get('/providers')
async def providers(request: Request):
    return await run_in_threadpool(provider_status, request.app.state.db, request.state.user.id)


@router.post('/pollinations/connect')
async def connect(request: Request):
    client, redirect = os.getenv('POLLINATIONS_APP_KEY'), os.getenv('POLLINATIONS_REDIRECT_URI')
    if not client or not redirect:
        raise HTTPException(503, 'Image-provider account connection is not configured yet')
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    await run_in_threadpool(lambda: request.app.state.db.table('provider_oauth_states').insert({
        'state_hash': hashlib.sha256(state.encode()).hexdigest(), 'user_id': request.state.user.id,
        'verifier_ciphertext': cipher().encrypt(verifier.encode()).decode(),
        'expires_at': (datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(),
    }).execute())
    return {'authorization_url': 'https://enter.pollinations.ai/authorize?' + urlencode({
        'client_id': client, 'redirect_uri': redirect, 'response_type': 'code',
        'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256',
        'models': 'image', 'budget': '1', 'expiry': '7',
    })}


class OAuthCode(BaseModel):
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=20, max_length=500)


@router.post('/pollinations/callback')
async def callback(payload: OAuthCode, request: Request):
    db, user_id = request.app.state.db, request.state.user.id
    # DELETE RETURNING consumes state atomically; it cannot be replayed or used
    # by another signed-in account. The verifier never visits the browser.
    rows = (await run_in_threadpool(lambda: db.table('provider_oauth_states').delete()
        .eq('state_hash', hashlib.sha256(payload.state.encode()).hexdigest())
        .eq('user_id', user_id).gt('expires_at', datetime.now(timezone.utc).isoformat()).execute())).data or []
    if not rows:
        raise HTTPException(400, 'Connection expired or state did not match; start again')
    verifier = cipher().decrypt(rows[0]['verifier_ciphertext'].encode()).decode()
    response = await run_in_threadpool(lambda: requests.post('https://enter.pollinations.ai/api/oauth/token', data={
        'grant_type': 'authorization_code', 'code': payload.code,
        'client_id': os.getenv('POLLINATIONS_APP_KEY'), 'redirect_uri': os.getenv('POLLINATIONS_REDIRECT_URI'),
        'code_verifier': verifier,
    }))
    if not response.ok:
        raise HTTPException(400, 'Image provider rejected the connection; start again')
    data = response.json()
    secret = data.get('access_token', '')
    if not secret.startswith('sk_'):
        raise HTTPException(502, 'Image provider returned an invalid authorization')
    expiry = (datetime.now(timezone.utc)+timedelta(seconds=int(data.get('expires_in', 604800)))).isoformat()
    await run_in_threadpool(save_credential, db, user_id, 'pollinations', secret, None, expiry)
    return {'status': 'connected'}


@router.delete('/pollinations')
async def disconnect(request: Request):
    await run_in_threadpool(lambda: request.app.state.db.table('provider_credentials').delete()
        .eq('user_id', request.state.user.id).eq('provider', 'pollinations').execute())
    return {'status': 'disconnected', 'message': 'Removed from GBP Master. You can also revoke the app in your Pollinations account.'}


class GenerateImage(BaseModel):
    request_id: UUID
    prompt: str = Field(min_length=5, max_length=2000)
    provider: str = Field(pattern='^(cloudflare|pollinations)$')


def image_bytes(response):
    if response.status_code in (401, 403):
        raise HTTPException(409, 'Image-provider authorization expired; reconnect it')
    if response.status_code in (402, 429):
        raise HTTPException(429, 'The provider allowance or budget is exhausted. Try after its reset or review your provider account.')
    if not response.ok:
        raise HTTPException(502, 'Image generation failed at the provider; try a new request later')
    chunks, length = [], 0
    for chunk in response.iter_content(65536):
        length += len(chunk)
        if length > MAX_BYTES * 2:
            raise HTTPException(502, 'Provider returned an oversized image')
        chunks.append(chunk)
    raw = b''.join(chunks)
    if 'json' in response.headers.get('content-type', ''):
        try:
            data = json.loads(raw)
            raw = base64.b64decode(data['result']['image'], validate=True)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(502, 'Provider did not return an image') from None
    return raw


def normalize_photo(raw):
    if len(raw) > MAX_BYTES:
        raise HTTPException(422, 'Photos must be smaller than 8 MB')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ('JPEG', 'PNG', 'WEBP') or image.width * image.height > 16_777_216:
                raise ValueError()
            image.load()
            if min(image.size) < 250:
                raise HTTPException(422, 'Photos must be at least 250 pixels on each side')
            image.thumbnail((2048, 2048))
            converted = image.convert('RGB')
            output = io.BytesIO()
            converted.save(output, 'JPEG', quality=92)
            return output.getvalue(), converted.width, converted.height
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, 'Upload a valid JPEG, PNG or WebP photo') from None


def generate_and_store(db, user_id, payload):
    fingerprint = hashlib.sha256(json.dumps({'provider':payload.provider,'prompt':payload.prompt}, sort_keys=True).encode()).hexdigest()
    params = {'p_user': user_id, 'p_request': str(payload.request_id), 'p_fingerprint': fingerprint}
    claimed = db.rpc('claim_image_generation', params).execute().data
    if not claimed:
        previous = db.table('image_generations').select('*').eq('user_id', user_id).eq('request_id', str(payload.request_id)).single().execute().data
        if previous['fingerprint'] != fingerprint:
            raise HTTPException(409, 'This request ID belongs to a different image')
        if previous['status'] == 'completed':
            asset = db.table('media_assets').select('*').eq('id', previous['asset_id']).eq('user_id',user_id).single().execute().data
            return {'asset': asset, 'replayed': True}
        raise HTTPException(409, 'This generation is already running or needs a new request')
    try:
        if payload.provider == 'cloudflare':
            account, key = os.getenv('CLOUDFLARE_ACCOUNT_ID'), os.getenv('CLOUDFLARE_API_TOKEN')
            if not account or not key:
                raise HTTPException(503, 'Shared image generation is not configured yet')
            response = requests.post(f'https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{MODEL}',
                headers={'Authorization': f'Bearer {key}'},
                files={'prompt': (None, payload.prompt), 'width': (None, '1024'), 'height': (None, '1024')},
                timeout=(5, 90), stream=True, allow_redirects=False)
        else:
            key = read_credential(db, user_id, 'pollinations')
            if not key:
                raise HTTPException(409, 'Connect your Pollinations account first')
            response = requests.get('https://gen.pollinations.ai/image/' + quote(payload.prompt, safe=''),
                headers={'Authorization': f'Bearer {key}'},
                params={'model': 'tongyi-mai/z-image-turbo', 'width':1024, 'height':1024, 'nologo':'true'},
                timeout=(5, 90), stream=True, allow_redirects=False)
        try:
            raw = image_bytes(response)
        finally:
            response.close()
        photo, width, height = normalize_photo(raw)
        path = f'{user_id}/generated/{payload.request_id}.jpg'
        storage = db.storage.from_('calendar_images')
        storage.upload(path, photo, {'content-type': 'image/jpeg', 'upsert': 'false'})
        asset = db.table('media_assets').insert({
            'user_id':user_id,'object_path':path,'public_url':storage.get_public_url(path),
            'storage_bucket':'calendar_images','media_kind':'photo','mime_type':'image/jpeg',
            'byte_size':len(photo),'width':width,'height':height,'status':'validated',
        }).execute().data[0]
        db.table('image_generations').update({'status':'completed','asset_id':asset['id']}).eq('user_id',user_id).eq('request_id',str(payload.request_id)).execute()
        return {'asset':asset,'replayed':False}
    except HTTPException:
        raise
    except PermissionError as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        # Do not retry provider calls automatically after an uncertain timeout.
        raise HTTPException(502, 'Image generation or saving failed. Check the media library before starting another request.') from None


@router.post('/generate')
async def generate(payload: GenerateImage, request: Request):
    return await run_in_threadpool(generate_and_store, request.app.state.db, request.state.user.id, payload)
