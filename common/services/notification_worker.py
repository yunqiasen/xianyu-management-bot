"""Persistent notification queue poller; runs only with the scheduler opt-in."""
import asyncio
from loguru import logger

async def run_notification_delivery():
    from common.db.session import async_session_maker
    from common.services.notification_delivery_service import NotificationDeliveryService
    while True:
        try:
            async with async_session_maker() as session:
                service = NotificationDeliveryService(session)
                await service.observe_account_states()
                await service.dispatch_due()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('通知投递队列本轮失败: {}', type(exc).__name__)
        await asyncio.sleep(10)
