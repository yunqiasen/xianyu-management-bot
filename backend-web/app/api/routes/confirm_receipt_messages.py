"""确认收货消息API路由

提供确认收货消息的增删改查接口
"""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from common.models.xy_account import XYAccount
from common.utils.auth_scope import resolve_owner_scope
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from common.models.confirm_receipt_message import ConfirmReceiptMessage
from common.models.user import User
from common.schemas.common import ApiResponse
from common.utils.local_image_upload import ImageUploadError, save_uploaded_image
from common.utils.time_utils import get_beijing_now_naive

router = APIRouter(tags=["confirm-receipt-messages"])

# 图片保存目录 - 使用统一的静态文件根目录（兼容Docker共享卷）
from app.core.paths import STATIC_ROOT
UPLOAD_DIR = STATIC_ROOT / "uploads" / "confirm_receipt"


async def _owned_account(db, user, account_id):
    owner_id, _ = resolve_owner_scope(user)
    statement = select(XYAccount).where(XYAccount.account_id == account_id)
    if owner_id is not None:
        statement = statement.where(XYAccount.owner_id == owner_id)
    account = await db.scalar(statement)
    if account is None:
        raise HTTPException(404, '账号不存在')
    return account


class ConfirmReceiptMessageResponse(BaseModel):
    """确认收货消息响应"""
    enabled: bool = False
    message_content: str = ""
    message_image: str = ""


class ConfirmReceiptMessageUpdate(BaseModel):
    """确认收货消息更新请求"""
    enabled: bool = False
    message_content: str = ""
    message_image: str = ""


@router.get("/{account_id}", response_model=ConfirmReceiptMessageResponse)
async def get_confirm_receipt_message(
    account_id: str,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """获取账号的确认收货消息配置"""
    await _owned_account(db, current_user, account_id)
    result = await db.execute(
        select(ConfirmReceiptMessage).where(ConfirmReceiptMessage.account_id == account_id)
    )
    message = result.scalar_one_or_none()
    
    if not message:
        return ConfirmReceiptMessageResponse()
    
    return ConfirmReceiptMessageResponse(
        enabled=message.enabled,
        message_content=message.message_content or "",
        message_image=message.message_image or "",
    )


@router.put("/{account_id}")
async def update_confirm_receipt_message(
    account_id: str,
    data: ConfirmReceiptMessageUpdate,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """更新账号的确认收货消息配置"""
    account = await _owned_account(db, current_user, account_id)
    if data.message_image and not data.message_image.startswith(('https://', 'http://')):
        from common.services.media_paths import owned_media_path
        try:
            owned_media_path(data.message_image, account.owner_id, static_root=STATIC_ROOT)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    result = await db.execute(
        select(ConfirmReceiptMessage).where(ConfirmReceiptMessage.account_id == account_id)
    )
    message = result.scalar_one_or_none()
    
    if message:
        # 更新现有记录
        message.enabled = data.enabled
        message.message_content = data.message_content
        message.message_image = data.message_image
        message.updated_at = get_beijing_now_naive()
    else:
        # 创建新记录
        message = ConfirmReceiptMessage(
            account_id=account_id,
            enabled=data.enabled,
            message_content=data.message_content,
            message_image=data.message_image,
        )
        db.add(message)
    
    await db.commit()

    return ApiResponse(success=True, message="保存成功")


@router.post("/{account_id}/upload-image")
async def upload_confirm_receipt_image(
    account_id: str,
    image: UploadFile = File(...),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """上传确认收货消息图片"""
    account = await _owned_account(db, current_user, account_id)
    try:
        _, filename, _ = await save_uploaded_image(
            image,
            UPLOAD_DIR / str(account.owner_id),
            filename_prefix=account_id,
            short_uuid=True,
        )
    except ImageUploadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    # 返回前端可访问的静态资源URL
    image_url = f"/static/uploads/confirm_receipt/{account.owner_id}/{filename}"

    return {"success": True, "image_url": image_url}
