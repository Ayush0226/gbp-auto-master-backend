"""Fenced cron leases. Public delivery claims are never released by recovery."""
import asyncio
import logging
from contextlib import suppress
from uuid import uuid4

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

log = logging.getLogger(__name__)
HEARTBEAT_SECONDS = 30


async def track_job(request: Request):
    if not request.url.path.startswith('/api/cron/'):
        yield
        return
    db = request.app.state.db
    if db is None:
        raise HTTPException(503, 'Database is not configured')
    key, owner = request.url.path, str(uuid4())

    async def rpc(name, **params):
        return await run_in_threadpool(lambda: db.rpc(name, params).execute().data)

    if not await rpc('claim_job_lease', p_key=key, p_owner=owner):
        raise HTTPException(409, 'Job is running or awaits reconciliation; inspect cron status')
    parent = asyncio.current_task()

    async def heartbeat():
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                if not await rpc('heartbeat_job_lease', p_key=key, p_owner=owner):
                    raise RuntimeError('Cron lease lost')
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Cron heartbeat failed for %s; stopping worker', key)
            parent.cancel()

    pulse = asyncio.create_task(heartbeat())
    status = 'failed'
    try:
        yield
        status = 'completed'
    finally:
        pulse.cancel()
        with suppress(asyncio.CancelledError):
            await pulse
        try:
            await rpc('finish_job_lease', p_key=key, p_owner=owner, p_status=status)
        except Exception:
            # Expiry allows the next worker to recover. Delivery-level publishing
            # guards stay held, even when a thread outlives a cancelled request.
            log.exception('Unable to finish cron lease for %s', key)
