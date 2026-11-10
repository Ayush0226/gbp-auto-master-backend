"""Shared web/MCP idempotency for paid upstream rank scans."""
import hashlib
import json
from starlette.concurrency import run_in_threadpool


async def measured_report(db, user_id, location_id, keyword, request_id, token_getter, scanner):
    operation='rank-report:'+user_id+':'+request_id
    fingerprint=hashlib.sha256(json.dumps({'location_id':location_id,'keyword':keyword},sort_keys=True).encode()).hexdigest()
    claim=await run_in_threadpool(lambda:db.rpc('claim_result_operation',{
        'p_operation':operation,'p_location':location_id,'p_user':user_id,'p_fingerprint':fingerprint,
    }).execute().data)
    if not claim.get('success'):
        raise ValueError(claim.get('error') or 'This scan is already running')
    if claim.get('replayed'):
        return claim['result']
    try:
        token=await token_getter()
        result=await run_in_threadpool(lambda:scanner(location_id=location_id,keyword=keyword,google_access_token=token))
    except Exception:
        await run_in_threadpool(lambda:db.rpc('finish_operation',{'p_operation':operation,'p_success':False}).execute())
        raise
    await run_in_threadpool(lambda:db.rpc('complete_result_operation',{'p_operation':operation,'p_result':result}).execute())
    return result
