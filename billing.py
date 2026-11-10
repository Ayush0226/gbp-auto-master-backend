"""Account access-code redemption. Payment checkout is temporarily disabled."""
import hashlib

from fastapi import HTTPException


def redeem_access_code(db, user_id, code):
    normalized = code.strip().upper()
    if not normalized or len(normalized) > 100 or not normalized.isascii():
        raise HTTPException(400, 'Invalid access code')
    digest = hashlib.sha256(normalized.encode('ascii')).hexdigest()
    result = db.rpc('redeem_lifetime_access', {
        'p_user': user_id, 'p_code_hash': digest,
    }).execute().data
    if not isinstance(result, dict) or not result.get('success'):
        raise HTTPException(400, 'This access code is invalid or unavailable')
    return result


def checkout_paused():
    raise HTTPException(503, 'Payment checkout is temporarily paused. Redeem an access code in Plans.')
