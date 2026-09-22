"""Internal version application; preserves account enablement and one live executor."""
from fastapi import APIRouter, Depends, HTTPException
from app.api.deps import require_internal_auth
from common.services.account_configuration import ConfigurationApplyRequest, ConfigurationError

router = APIRouter(prefix='/internal/account-configuration', dependencies=[Depends(require_internal_auth)])


@router.post('')
async def apply_configuration(request: ConfigurationApplyRequest):
    from common.db.session import async_session_maker
    from app.services.xianyu.cookie_manager import get_manager
    try:
        return await get_manager().apply_configuration(request, async_session_maker)
    except ConfigurationError as exc:
        raise HTTPException(exc.status, detail={'code':exc.code}) from exc
