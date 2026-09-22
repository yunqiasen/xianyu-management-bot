from __future__ import annotations

from typing import Dict

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.api import deps
from common.models.user import User
from common.schemas.ai_reply import AIModelListRequest, AIReplySettings, AIReplySettingsUpdate, AIPresetCreate, AIPresetUpdate, AIPresetApply
from common.schemas.common import ApiResponse
from common.services.ai_provider_service import fetch_ai_model_list, test_ai_connection
from common.utils.auth_scope import resolve_owner_scope
from app.services.account_service import AccountService
from app.services.ai_reply_service import AIReplySettingsService, public_ai_settings
from app.services.ai_preset_service import AIPresetService
from common.services.ai_gateway import AIGatewayError, is_secret_placeholder

router = APIRouter(tags=["ai"])
test_router = APIRouter(prefix="/ai-reply-test", tags=["ai"])


@router.get("", response_model=dict[str, AIReplySettings])
async def list_ai_reply_settings(
    current_user: User = Depends(deps.get_current_active_user),
    ai_service: AIReplySettingsService = Depends(deps.get_ai_reply_service),
) -> dict[str, AIReplySettings]:
    return {key: public_ai_settings(value) for key, value in (await ai_service.list_settings(current_user.id)).items()}


@router.post("/models", response_model=ApiResponse)
async def fetch_ai_reply_models(
    payload: AIModelListRequest,
    current_user: User = Depends(deps.get_current_active_user),
    account_service: AccountService = Depends(deps.get_account_service),
    ai_service: AIReplySettingsService = Depends(deps.get_ai_reply_service),
) -> ApiResponse:
    """手动获取AI模型列表，失败时返回空列表，由前端切换为手动输入"""
    try:
        key = payload.api_key
        if is_secret_placeholder(key) or not key:
            if not payload.account_id:
                raise ValueError("请选择保存密钥的账号")
            account = await account_service.get_account_for_user(current_user.id, payload.account_id)
            if not account:
                raise ValueError("账号不存在")
            key = (await ai_service.get_settings(account))["api_key"]
        models = await fetch_ai_model_list(
            payload.provider_type,
            payload.base_url,
            key,
        )
        if not models:
            return ApiResponse(
                success=False,
                message="该服务商未返回模型列表，请直接在文本框输入模型名称",
                data={"models": []},
            )
        return ApiResponse(success=True, message=f"获取模型列表成功，共 {len(models)} 个模型", data={"models": models})
    except (TimeoutError, httpx.TimeoutException):
        return ApiResponse(
            success=False,
            message="获取模型列表超时，请直接在文本框输入模型名称",
            data={"models": []},
        )
    except Exception as e:
        return ApiResponse(
            success=False,
            message="获取模型列表失败，请检查协议配置或手动输入模型名称",
            data={"models": []},
        )


async def get_preset_service(session=Depends(deps.get_db_session)) -> AIPresetService:
    return AIPresetService(session)


@router.get("/presets", response_model=ApiResponse)
async def list_ai_presets(current_user: User = Depends(deps.get_current_active_user), service: AIPresetService = Depends(get_preset_service)) -> ApiResponse:
    return ApiResponse(success=True, data=await service.list(current_user.id))


@router.post("/presets", response_model=ApiResponse)
async def create_ai_preset(payload: AIPresetCreate, current_user: User = Depends(deps.get_current_active_user), service: AIPresetService = Depends(get_preset_service)) -> ApiResponse:
    try:
        data = await service.create(current_user.id, payload.name, payload.settings.model_dump(exclude_unset=True), source_account_id=payload.source_account_id)
        return ApiResponse(success=True, message="预设已保存", data=data)
    except ValueError as exc:
        await service.session.rollback()
        return ApiResponse(success=False, message=str(exc))


@router.put("/presets/{preset_id}", response_model=ApiResponse)
async def update_ai_preset(preset_id: str, payload: AIPresetUpdate, current_user: User = Depends(deps.get_current_active_user), service: AIPresetService = Depends(get_preset_service)) -> ApiResponse:
    try:
        settings = payload.settings.model_dump(exclude_unset=True) if payload.settings is not None else None
        return ApiResponse(success=True, message="预设已更新", data=await service.update(current_user.id, preset_id, name=payload.name, settings=settings))
    except ValueError as exc:
        await service.session.rollback()
        return ApiResponse(success=False, message=str(exc))


@router.delete("/presets/{preset_id}", response_model=ApiResponse)
async def delete_ai_preset(preset_id: str, current_user: User = Depends(deps.get_current_active_user), service: AIPresetService = Depends(get_preset_service)) -> ApiResponse:
    try:
        await service.delete(current_user.id, preset_id)
        return ApiResponse(success=True, message="预设已删除，已应用账号保持原配置")
    except ValueError as exc:
        await service.session.rollback()
        return ApiResponse(success=False, message=str(exc))


@router.post("/presets/{preset_id}/apply", response_model=ApiResponse)
async def apply_ai_preset(preset_id: str, payload: AIPresetApply, current_user: User = Depends(deps.get_current_active_user), service: AIPresetService = Depends(get_preset_service)) -> ApiResponse:
    try:
        results = await service.apply(current_user.id, preset_id, payload.account_ids)
        count = sum(row["success"] for row in results)
        return ApiResponse(success=all(row["success"] for row in results), message=f"已应用 {count}/{len(results)} 个账号", data={"results": results})
    except ValueError as exc:
        await service.session.rollback()
        return ApiResponse(success=False, message=str(exc))


@router.get("/{cookie_id}/bargaining-diagnostics", response_model=ApiResponse)
async def bargaining_diagnostics(
    cookie_id: str,
    current_user: User = Depends(deps.get_current_active_user),
    account_service: AccountService = Depends(deps.get_account_service),
    session=Depends(deps.get_db_session),
) -> ApiResponse:
    """P5 候选诊断：读取实际回复日志，不返回消息正文、配置或原始快照。"""
    from sqlalchemy import select
    from common.core.config import get_settings
    from common.models.auto_reply_message_log import XYAutoReplyMessageLog
    from common.services.bargaining_history import public_diagnostics

    account = await account_service.get_account_for_user(current_user.id, cookie_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    if get_settings().xymb_enable_bargaining_v2 is not True:
        return ApiResponse(success=True, data={"candidate_enabled": False, "records": []})
    rows = (await session.execute(select(XYAutoReplyMessageLog).where(
        XYAutoReplyMessageLog.account_id == cookie_id,
        XYAutoReplyMessageLog.owner_id == account.owner_id,
    ).order_by(XYAutoReplyMessageLog.id.desc()).limit(100))).scalars().all()
    records = []
    for row in rows:
        snapshot = row.context_snapshot or {}
        diagnostics = public_diagnostics(snapshot.get("ai_diagnostics"))
        if diagnostics:
            records.append({"log_id": row.id, "chat_id": row.chat_id, **diagnostics})
        if len(records) == 20:
            break
    return ApiResponse(success=True, data={"candidate_enabled": True, "records": records})


@router.get("/{cookie_id}", response_model=AIReplySettings)
async def get_ai_reply_settings(
    cookie_id: str,
    current_user: User = Depends(deps.get_current_active_user),
    account_service: AccountService = Depends(deps.get_account_service),
    ai_service: AIReplySettingsService = Depends(deps.get_ai_reply_service),
) -> AIReplySettings:
    # 管理员可以操作所有账号，普通用户只能操作自己的账号
    owner_id, _ = resolve_owner_scope(current_user)
    account = await account_service.get_account_for_user(owner_id, cookie_id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    return AIReplySettings(**public_ai_settings(await ai_service.get_settings(account)))


@router.put("/{cookie_id}", response_model=ApiResponse)
async def update_ai_reply_settings(
    cookie_id: str,
    payload: AIReplySettingsUpdate,
    current_user: User = Depends(deps.get_current_active_user),
    account_service: AccountService = Depends(deps.get_account_service),
    ai_service: AIReplySettingsService = Depends(deps.get_ai_reply_service),
) -> ApiResponse:
    # 管理员可以操作所有账号，普通用户只能操作自己的账号
    owner_id, _ = resolve_owner_scope(current_user)
    account = await account_service.get_account_for_user(owner_id, cookie_id)
    if not account:
        return ApiResponse(success=False, message="账号不存在")
    try:
        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            return ApiResponse(success=False, message="请至少填写一个修改项")
        saved = await ai_service.update_settings(account, update_data)
    except ValueError as e:
        await ai_service.session.rollback()
        return ApiResponse(success=False, message=str(e))
    return ApiResponse(success=True, message="AI回复设置更新成功", data=public_ai_settings(saved))


@router.put("", response_model=ApiResponse)
async def bulk_update_ai_settings(
    settings_map: Dict[str, AIReplySettingsUpdate] = Body(...),
    current_user: User = Depends(deps.get_current_active_user),
    account_service: AccountService = Depends(deps.get_account_service),
    ai_service: AIReplySettingsService = Depends(deps.get_ai_reply_service),
) -> ApiResponse:
    results = []
    for cookie_id, payload in settings_map.items():
        try:
            account = await account_service.get_account_for_user(current_user.id, cookie_id)
            if account is None:
                raise ValueError("账号不存在")
            update_data = payload.model_dump(exclude_unset=True)
            if not update_data:
                raise ValueError("请至少填写一个修改项")
            saved = await ai_service.update_settings(account, update_data)
            results.append({"account_id": cookie_id, "success": True, "config_version": saved["config_version"]})
        except Exception as exc:
            await ai_service.session.rollback()
            results.append({"account_id": cookie_id, "success": False, "message": str(exc) if isinstance(exc, ValueError) else "账号配置写入失败"})
    count = sum(row["success"] for row in results)
    return ApiResponse(success=bool(results) and all(row["success"] for row in results), message=f"已更新 {count}/{len(results)} 个账号", data={"results": results})


@test_router.post("/{cookie_id}", response_model=ApiResponse)
async def test_ai_reply_settings(
    cookie_id: str,
    current_user: User = Depends(deps.get_current_active_user),
    account_service: AccountService = Depends(deps.get_account_service),
    ai_service: AIReplySettingsService = Depends(deps.get_ai_reply_service),
) -> ApiResponse:
    """测试AI连接是否正常"""
    # 管理员可以操作所有账号，普通用户只能操作自己的账号
    owner_id, _ = resolve_owner_scope(current_user)
    account = await account_service.get_account_for_user(owner_id, cookie_id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    
    # 获取该账号的AI设置
    settings = await ai_service.get_settings(account)
    api_key = settings.get("api_key", "")
    provider_type = settings.get("provider_type", "openai_compatible")
    base_url = settings.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    model_name = settings.get("model_name", "qwen-plus")
    
    try:
        reply = await test_ai_connection(provider_type, base_url, api_key, model_name, settings=settings, return_result=True)
        return ApiResponse(success=True, message="AI连接测试成功", data={"stage": reply.stage, "reply": reply.text, "call_id": reply.call_id, "config_version": reply.config_version, "trimmed_messages": reply.trimmed_messages})
    except AIGatewayError as exc:
        return ApiResponse(success=False, message=str(exc), data=exc.public_data())
    except (TimeoutError, httpx.TimeoutException):
        return ApiResponse(success=False, message="AI连接超时，请检查网络或API地址", data={"stage": "timeout"})
    except Exception as e:
        return ApiResponse(success=False, message="AI连接测试失败", data={"stage": "internal"})
