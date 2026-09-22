from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.api import deps
from common.models.user import User
from common.schemas.common import ApiResponse
from common.schemas.notification import (
    MessageNotificationSet,
    NotificationChannelCreate,
    NotificationChannelUpdate,
)
from app.services.notification_service import (
    MessageNotificationService,
    NotificationChannelService,
)

channels_router = APIRouter(prefix="/notification-channels", tags=["notifications"])
messages_router = APIRouter(prefix="/message-notifications", tags=["notifications"])


@channels_router.get("")
async def list_notification_channels(
    current_user: User = Depends(deps.get_current_active_user),
    service: NotificationChannelService = Depends(deps.get_notification_channel_service),
) -> list[dict]:
    return await service.list_channels(current_user.id)


@channels_router.post("", response_model=ApiResponse)
async def create_notification_channel(
    payload: NotificationChannelCreate,
    current_user: User = Depends(deps.get_current_active_user),
    service: NotificationChannelService = Depends(deps.get_notification_channel_service),
) -> ApiResponse:
    try:
        await service.create_channel(current_user.id, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return ApiResponse(success=True, message="通知渠道已创建")


@channels_router.put("/{channel_id}", response_model=ApiResponse)
async def update_notification_channel(
    channel_id: int,
    payload: NotificationChannelUpdate,
    current_user: User = Depends(deps.get_current_active_user),
    service: NotificationChannelService = Depends(deps.get_notification_channel_service),
) -> ApiResponse:
    try:
        updated = await service.update_channel(current_user.id, channel_id, payload)
    except ValueError as exc:
        await service.session.rollback()
        raise HTTPException(422, str(exc))
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="通知渠道不存在")
    return ApiResponse(success=True, message="通知渠道已更新")


@channels_router.delete("/{channel_id}", response_model=ApiResponse)
async def delete_notification_channel(
    channel_id: int,
    current_user: User = Depends(deps.get_current_active_user),
    service: NotificationChannelService = Depends(deps.get_notification_channel_service),
) -> ApiResponse:
    deleted = await service.delete_channel(current_user.id, channel_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="通知渠道不存在")
    return ApiResponse(success=True, message="通知渠道已删除")


@channels_router.post("/{channel_id}/test", response_model=ApiResponse)
async def test_notification_channel(
    channel_id: int,
    current_user: User = Depends(deps.get_current_active_user),
    service: NotificationChannelService = Depends(deps.get_notification_channel_service),
) -> ApiResponse:
    from common.services.notification_delivery_service import NotificationDeliveryService
    from common.services.notification_template_service import NotificationTemplateService
    from common.models.notification_delivery import NotificationEvent, NotificationDelivery
    from uuid import uuid4
    import time
    channel = await service.get_channel(current_user.id, channel_id)
    if not channel:
        raise HTTPException(404, "通知渠道不存在")
    event_id = str(uuid4())
    payload = {'summary': '通知渠道测试', 'account_id': '-'}
    rendered, failed = await NotificationTemplateService(service.session).render_event(current_user.id, 'test', payload)
    event = NotificationEvent(id=event_id, owner_id=current_user.id, account_id='-', event_type='test', generation=event_id, payload=payload, first_seen=time.time(), last_seen=time.time())
    delivery = NotificationDelivery(event_id=event_id, owner_id=current_user.id, channel_id=channel_id, rendered=rendered, template_failed=failed, due_at=time.time())
    service.session.add_all([event, delivery]); await service.session.commit()
    await NotificationDeliveryService(service.session).dispatch_due(current_user.id,event_id=event_id)
    await service.session.refresh(delivery)
    return ApiResponse(success=delivery.status == 'accepted', message={'accepted':'渠道已受理','not_accepted':'渠道明确未受理，将按计划重试','unknown':'受理结果待核实，不自动重发','cancelled':'渠道已停用'}.get(delivery.status,delivery.status), data={'status':delivery.status,'delivery_id':delivery.id})


@messages_router.get("")
async def list_message_notifications(
    current_user: User = Depends(deps.get_current_active_user),
    service: MessageNotificationService = Depends(deps.get_message_notification_service),
) -> dict:
    return await service.list_notifications(current_user.id)


@messages_router.get("/{cookie_id}")
async def list_message_notifications_for_account(
    cookie_id: str,
    current_user: User = Depends(deps.get_current_active_user),
    service: MessageNotificationService = Depends(deps.get_message_notification_service),
) -> list[dict]:
    return await service.list_for_account(current_user.id, cookie_id)


@messages_router.post("/{cookie_id}", response_model=ApiResponse)
async def set_message_notification(
    cookie_id: str,
    payload: MessageNotificationSet,
    current_user: User = Depends(deps.get_current_active_user),
    service: MessageNotificationService = Depends(deps.get_message_notification_service),
) -> ApiResponse:
    success = await service.set_notification(current_user.id, cookie_id, payload)
    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号或通知渠道不存在")
    return ApiResponse(success=True, message="消息通知配置已更新")


@messages_router.delete("/{notification_id}", response_model=ApiResponse)
async def delete_message_notification(
    notification_id: int,
    current_user: User = Depends(deps.get_current_active_user),
    service: MessageNotificationService = Depends(deps.get_message_notification_service),
) -> ApiResponse:
    deleted = await service.delete_subscription(current_user.id, notification_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息通知不存在")
    return ApiResponse(success=True, message="消息通知已删除")


@messages_router.delete("/account/{cookie_id}", response_model=ApiResponse)
async def delete_account_notifications(
    cookie_id: str,
    current_user: User = Depends(deps.get_current_active_user),
    service: MessageNotificationService = Depends(deps.get_message_notification_service),
) -> ApiResponse:
    deleted = await service.delete_for_account(current_user.id, cookie_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在或没有通知配置")
    return ApiResponse(success=True, message="账号通知配置已清理")


from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

class TemplatePayload(BaseModel):
    body: str = Field(min_length=1,max_length=4000)
    variables: dict = Field(default_factory=dict)

@channels_router.get('/templates')
async def list_templates(user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    from common.services.notification_template_service import NotificationTemplateService
    return {'success':True,'data':await NotificationTemplateService(session).list(user.id)}

@channels_router.put('/templates/{event_type}')
async def save_template(event_type: str, payload: TemplatePayload, user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    from common.services.notification_template_service import NotificationTemplateService
    try: await NotificationTemplateService(session).save(user.id,event_type,payload.body)
    except ValueError as exc: raise HTTPException(422,str(exc))
    return {'success':True}

@channels_router.delete('/templates/{event_type}')
async def reset_template(event_type: str, user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    from common.services.notification_template_service import NotificationTemplateService
    try: await NotificationTemplateService(session).reset(user.id,event_type)
    except ValueError as exc: raise HTTPException(422,str(exc))
    return {'success':True}

@channels_router.post('/templates/{event_type}/preview')
async def preview_template(event_type: str, payload: TemplatePayload, user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    from common.services.notification_template_service import NotificationTemplateService
    svc=NotificationTemplateService(session)
    text,failed=svc.render(event_type,payload.body,payload.variables)
    return {'success':not failed,'text':text,'template_failed':failed}

@channels_router.get('/deliveries')
async def list_deliveries(limit: int = 50, offset: int = 0, user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    from common.models.notification_delivery import NotificationDelivery, NotificationEvent
    from sqlalchemy import select
    rows=(await session.execute(select(NotificationDelivery,NotificationEvent).join(NotificationEvent,NotificationEvent.id==NotificationDelivery.event_id).where(NotificationDelivery.owner_id==user.id).order_by(NotificationDelivery.created_at.desc()).limit(max(1,min(limit,100))).offset(max(0,offset)))).all()
    return {'success':True,'data':[{'id':d.id,'event_id':e.id,'account_id':e.account_id,'event_type':e.event_type,'merged_count':e.merged_count,'channel_id':d.channel_id,'status':d.status,'attempts':d.attempts,'due_at':d.due_at,'template_failed':d.template_failed} for d,e in rows]}
