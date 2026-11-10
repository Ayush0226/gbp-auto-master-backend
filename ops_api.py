"""Read-only operational checks. Access is restricted by the global auth guard."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

router = APIRouter(tags=['operations'])
EXPECTED_JOBS = {
    '/api/cron/publish-scheduled': 15,
    '/api/cron/reply-reviews': 30,
    '/api/cron/scrub-calendar': 26 * 60,
}


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def inspect_ops(db):
    now = datetime.now(timezone.utc)
    rows = db.table('job_runs').select('*').execute().data or []
    known = {row['job_key']: row for row in rows}
    jobs, issues = [], []
    for key, minutes in EXPECTED_JOBS.items():
        row = known.get(key, {})
        updated = timestamp(row.get('updated_at'))
        healthy = row.get('status') == 'completed' and updated and now-updated < timedelta(minutes=minutes)
        if row.get('status') == 'running':
            expires = timestamp(row.get('lease_expires_at'))
            healthy = bool(expires and expires > now)
        if not healthy:
            issues.append(f'{key}: missing, stale, failed, or awaiting reconciliation')
        jobs.append({'job_key':key,'status':row.get('status','missing'), 'updated_at':row.get('updated_at'),
            'lease_expires_at':row.get('lease_expires_at'),'healthy':bool(healthy),'detail':row.get('detail')})
    held = {}
    for table in ('calendar_posts','campaign_locations','review_reply_jobs'):
        data = db.table(table).select('*',count='exact').eq('status','publishing').limit(0).execute()
        held[table] = data.count or 0
    if sum(held.values()):
        issues.append('Held Google deliveries require reconciliation before retrying')
    inbound = db.table('inbound_events').select('processed_at').eq('status','completed').order('processed_at',desc=True).limit(1).execute().data or []
    return {'healthy':not issues,'checked_at':now.isoformat(),'jobs':jobs,'held_deliveries':held,
        'last_review_event_at':inbound[0].get('processed_at') if inbound else None,'issues':issues}


@router.get('/api/admin/operations')
async def operations(request: Request):
    return await run_in_threadpool(inspect_ops, request.app.state.db)


@router.get('/api/cron/monitor')
async def monitor(request: Request):
    result = await run_in_threadpool(inspect_ops, request.app.state.db)
    if not result['healthy']:
        raise HTTPException(503, result)
    return result
