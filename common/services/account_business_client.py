"""Shared caller adapter for the worker's fixed business commands."""
from uuid import uuid4
from common.services.account_dispatch import AccountDispatchClient, DispatchError


async def dispatch_business(account, command, payload, *, request_id=None, rpc=None, sessions=None):
    if sessions is None:
        from common.db.session import async_session_maker
        sessions=async_session_maker
    if rpc is None:
        from app.core.config import get_settings
        settings=get_settings()
        rpc=AccountDispatchClient(settings.websocket_service_url,settings.internal_api_token)
    try:
        async with sessions() as session:
            result=await rpc.submit(owner_id=account.owner_id,account_id=account.account_id,
                request_id=request_id or 'business-'+uuid4().hex,command=command,payload=payload,session=session)
        return {'success':result.status=='confirmed','definitive_failure':result.status=='failed',
                'unknown':result.status in {'unknown','submitted'},
                'message':result.error_code or result.status,'result':result.result,
                'retry_after':result.retry_after}
    except DispatchError as exc:
        return {'success':False,'definitive_failure':True,'unknown':False,'message':exc.code}
