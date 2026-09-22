"""内部账号操作 RPC；主程序负责 include_router，保留现有公共路由。"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from common.services.account_dispatch import DispatchError, DispatchRequest, OperationResult
from app.api.deps import require_internal_auth

router = APIRouter(prefix='/internal/account-operations', tags=['internal'],
                   dependencies=[Depends(require_internal_auth)])


def get_account_dispatcher(request: Request):
    dispatcher = getattr(request.app.state, 'account_dispatcher', None)
    if dispatcher is None:
        raise HTTPException(503, detail={'code': 'dispatcher_not_ready'})
    return dispatcher


@router.post('', response_model=OperationResult)
async def execute_operation(body: DispatchRequest, dispatcher=Depends(get_account_dispatcher)):
    try: return await dispatcher.execute(body)
    except DispatchError as exc:
        raise HTTPException(exc.http_status, detail={'code': exc.code}) from exc


@router.get('/{request_id}', response_model=OperationResult)
async def get_operation(request_id: str, owner_id: int = Query(gt=0),
                        account_id: str = Query(min_length=1, max_length=80),
                        dispatcher=Depends(get_account_dispatcher)):
    try: return await dispatcher.get_operation(owner_id, account_id, request_id)
    except DispatchError as exc:
        raise HTTPException(exc.http_status, detail={'code': exc.code}) from exc


from common.services.account_renewal import RenewalRequest, renew_on_executor


@router.post('/renew')
async def renew_account(body: RenewalRequest, dispatcher=Depends(get_account_dispatcher)):
    try:
        return await renew_on_executor(dispatcher, body)
    except DispatchError as exc:
        raise HTTPException(exc.http_status, detail={'code':exc.code}) from exc


@router.get('/executors/{account_id}')
async def get_executor_status(account_id: str, owner_id: int = Query(gt=0),
                              dispatcher=Depends(get_account_dispatcher)):
    try:
        return await dispatcher.inspect_executor(owner_id, account_id)
    except DispatchError as exc:
        raise HTTPException(exc.http_status, detail={'code': exc.code}) from exc
