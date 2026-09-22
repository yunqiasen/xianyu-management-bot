"""调度器 -> 本地ASGI旧API桥 -> 真实SQL履约；不访问平台。"""
import importlib.util
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.models.delivery_intent import DeliveryIntent
from common.models.card import Card
import ws_legacy
# scheduler与websocket共享app名称；仅装载调度器文件，网络入口使用本地HTTP适配器。
http_module=ModuleType('app.core.http_client');http_module.get_http_client=lambda:None
sys.modules.setdefault('app.core.http_client',http_module)
spec=importlib.util.spec_from_file_location('commerce_redelivery','scheduler/app/services/scheduler/redelivery_task.py')
scheduler=importlib.util.module_from_spec(spec);spec.loader.exec_module(scheduler)

class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await ws_legacy.LegacyTests.asyncSetUp(self)
        self.network_guard=patch('common.services.order_service.OrderStatusChecker._fetch_raw_order_detail',AsyncMock(return_value=None))
        self.network_guard.start();self.addCleanup(self.network_guard.stop)

    async def asyncTearDown(self): await self.engine.dispose()

    async def test_cooldown_identity_isolated(self):
        scheduler._order_cooldown_cache.clear()
        scheduler.add_order_to_cooldown((1,'a','same'))
        self.assertFalse(scheduler.is_order_in_cooldown((2,'b','same')))

    async def test_persisted_intent_precedes_redis_and_old_status_check(self):
        from app.services.shipping import legacy_bridge,commerce_routes
        from app.api.deps import require_internal_auth
        from common.models.scheduled_redelivery_log import ScheduledRedeliveryLog
        async with self.engine.begin() as connection: await connection.run_sync(ScheduledRedeliveryLog.__table__.create)
        async with self.sessions() as session:
            account=await session.get(XYAccount,1);account.scheduled_redelivery=True;account.auto_confirm=True;await session.commit()
        row=await self.service.reserve(1,'a','same',1)
        await self.service.execute(row.id,1,send=AsyncMock(return_value='confirmed'),confirm=None)
        app=FastAPI();app.include_router(commerce_routes.router);app.dependency_overrides[require_internal_auth]=lambda:None
        class Request(BaseModel):
            owner_id:int;account_id:str;order_no:str;item_id:str;buyer_id:str;chat_id:str;card_id:int|None=None
        @app.post('/internal/orders/deliver')
        async def deliver(body:Request): return await legacy_bridge.maybe_deliver(body)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
            async def post(url,json,**kwargs): return (await client.post(url,json=json)).json()
            with patch.object(scheduler,'async_session_maker',self.sessions),patch.object(scheduler.RedeliveryTask,'ORDER_PROCESS_DELAY',0),patch.object(legacy_bridge,'async_session_maker',self.sessions),patch.object(commerce_routes,'async_session_maker',self.sessions),patch.object(scheduler,'get_http_client',return_value=SimpleNamespace(post=post)),patch.object(scheduler,'get_settings',return_value=SimpleNamespace(websocket_service_url='http://fixture')),patch('app.services.xianyu.cookie_manager.get_manager',return_value=SimpleNamespace(instances={'a':self.live})),patch('common.db.redis_client.try_acquire_delivery_lock',AsyncMock(return_value=SimpleNamespace(success=False,is_locked_by_other=True))),patch('common.services.order_service.check_can_ship',AsyncMock(side_effect=AssertionError('已发内容禁止走旧预检'))):
                scheduler.add_order_to_cooldown((1,'a','same'))
                await scheduler.RedeliveryTask().execute()
        self.handler.send_msg.assert_not_called()
        from sqlalchemy import select
        async with self.sessions() as session:
            log=await session.scalar(select(ScheduledRedeliveryLog))
            self.assertIsNotNone(log);self.assertEqual(log.status,'success')
        async with self.sessions() as s:
            self.assertEqual((await s.get(DeliveryIntent,row.id)).confirm_state,'confirmed')
            self.assertEqual((await s.get(Card,1)).data_content,'C')

    async def test_accepted_task_does_not_mark_order_shipped(self):
        from common.models.card import Card
        scheduler._order_cooldown_cache.clear()
        lock=SimpleNamespace(success=True,is_locked_by_other=False)
        with patch.object(scheduler,'get_settings',return_value=SimpleNamespace(websocket_service_url='http://fixture')),patch.object(scheduler,'get_http_client',return_value=SimpleNamespace(post=AsyncMock(return_value={'success':True,'data':{'status':'submitted'}}))),patch('common.db.redis_client.try_acquire_delivery_lock',AsyncMock(return_value=lock)),patch('common.db.redis_client.release_delivery_lock',AsyncMock()),patch('common.services.order_service.check_can_ship',AsyncMock(return_value={'success':True,'can_ship':True})),patch.object(scheduler.RedeliveryTask,'_get_matching_card',AsyncMock(return_value=SimpleNamespace(id=1))):
            async with self.sessions() as s:
                account=await s.get(XYAccount,1);order=await s.get(XYOrder,1)
                order.spec_name='颜色';order.spec_value='蓝'
                success,_,_=await scheduler.RedeliveryTask()._process_order(s,account,order)
                await s.refresh(order)
                self.assertFalse(success);self.assertEqual(order.status,'pending_ship')

    async def test_old_shipped_order_with_pending_content_is_selected(self):
        row=await self.service.reserve(1,'a','same',1,mode='confirm_first')
        await self.service.execute(row.id,1,send=None,confirm=AsyncMock(return_value='confirmed'))
        async with self.sessions() as session:
            orders=await scheduler.RedeliveryTask()._get_pending_orders(session,'a')
            self.assertEqual([o.order_no for o in orders],['same'])

if __name__=='__main__':unittest.main()
