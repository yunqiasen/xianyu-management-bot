"""Scheduler account settings are read from SQL per job; acknowledge only the exact version."""
from fastapi import APIRouter, Depends, HTTPException
from app.api.deps import require_internal_auth
from common.services.account_configuration import ConfigurationApplyRequest, ConfigurationStore, ConfigurationError

router = APIRouter(prefix='/internal/account-configuration', dependencies=[Depends(require_internal_auth)])


@router.post('')
async def apply_configuration(request: ConfigurationApplyRequest):
    try:
        return await ConfigurationStore().acknowledge(request.owner_id, request.account_id,
                                                     request.config_version, 'scheduler')
    except ConfigurationError as exc:
        raise HTTPException(exc.status, detail={'code':exc.code}) from exc
