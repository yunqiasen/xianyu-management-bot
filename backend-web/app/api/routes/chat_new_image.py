"""
在线聊天(新) - 图片发送 API 路由

功能：
1. 接收前端上传的图片，先上传到闲鱼CDN，再通过IM协议发送图片消息给买家

与 chat_new.py 共用同一 prefix="/chat-new"，按功能拆分为独立 router 以控制单文件体积。
"""
from __future__ import annotations

import os
import tempfile
import uuid
from common.services.reply_state import ReplyState, platform_send_result
from common.services.reply_images import ReplyImages, MAX_BYTES

from fastapi import APIRouter, Depends, File, Form, UploadFile
from loguru import logger
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_user, get_db_session
from app.services.chat_new import get_im_session_manager
from common.models import User, XYAccount
from common.schemas.common import ApiResponse
from common.utils.auth_scope import is_admin_user
from common.utils.image_uploader import ImageUploader

router = APIRouter(prefix="/chat-new")

# 允许的图片类型与大小上限（与项目其它图片上传保持一致）
_ALLOWED_CONTENT_PREFIX = "image/"
_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB


async def _get_owned_chat_account(
    account_id: str, current_user: User, db: AsyncSession
) -> XYAccount | None:
    """校验账号归属：管理员可操作任意账号，普通用户仅能操作自己的账号"""
    query = select(XYAccount).where(XYAccount.account_id == account_id)
    if not is_admin_user(current_user):
        query = query.where(XYAccount.owner_id == current_user.id)
    return (await db.execute(query)).scalar_one_or_none()


@router.post("/send-image/{account_id}")
async def send_image(
    account_id: str,
    cid: str = Form(...),
    toUserId: str = Form(...),
    requestId: str = Form(default=''),
    image: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    account = await _get_owned_chat_account(account_id, current_user, db)
    if not account:
        return ApiResponse(success=False, message="账号不存在或无权操作")
    client = get_im_session_manager().clients.get(account_id)
    if not client or not client.is_connected:
        return ApiResponse(success=False, message="账号未连接")
    request_id = requestId or uuid.uuid4().hex
    if len(request_id) > 128:
        return ApiResponse(success=False, message="请求身份过长")
    store = ReplyState()
    images = ReplyImages(store.sessions)
    try:
        resource = await images.upload(account.owner_id, await image.read(MAX_BYTES + 1))
        await images.reference(account.owner_id, resource['id'], 'chat', request_id)
    except ValueError as exc:
        return ApiResponse(success=False, message=str(exc))
    async def transport():
        cdn_url = await client.upload_image(str(images.root / resource['path']), request_id=request_id)
        if not cdn_url:
            return {'status': 'failed', 'error': '图片上传失败，消息未发送'}
        result = await client.send_image_message(cid=cid, to_user_id=toUserId, image_url=cdn_url,
            width=resource['width'], height=resource['height'], request_id=request_id)
        return {**platform_send_result(result), 'imageUrl': cdn_url}
    try:
        result = await store.send(account_id, cid, request_id, resource['url'], transport,
            pause_minutes=account.pause_duration, content_type='image', fingerprint=toUserId, sender_id=client.myid)
    except ValueError as exc:
        return ApiResponse(success=False, message=str(exc))
    labels = {'submitted': '已提交', 'confirmed': '平台已确认', 'failed': '明确失败', 'unknown': '结果待核实，请先核对聊天记录'}
    return ApiResponse(success=result['status'] != 'failed', message=labels[result['status']], data=result)
