from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from credential_store import protect_google_grant

router = APIRouter(prefix='/api/connections', tags=['connections'])


class GoogleGrant(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=8192)


@router.post('/google')
async def save_google_grant(payload: GoogleGrant, request: Request):
    try:
        await run_in_threadpool(protect_google_grant, request.app.state.db, request.state.user, payload.refresh_token)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from None
    return {'status': 'connected'}
