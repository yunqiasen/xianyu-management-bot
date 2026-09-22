"""独立 websocket 命名空间：真实 AutoDeliveryHandler -> 持久链。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import test_runtime as fixtures
from app.services.xianyu.auto_delivery_handler import AutoDeliveryHandler
from common.models.delivery_intent import DeliveryIntent
from sqlalchemy import select

class WebsocketPaymentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self): await fixtures.RuntimeTests.asyncSetUp(self)
    async def asyncTearDown(self): await self.engine.dispose()
    async def test_payment_handler_runs_persistent_chain_and_replay_is_noop(self):
        parent=SimpleNamespace(cookie_id='a',ws=object(),send_msg=AsyncMock(return_value={'delivery_state':'confirmed'}),
            send_image_msg=AsyncMock(),is_only_send_card_enabled=lambda:False,
            get_agree_deliver_config=lambda:{},is_auto_confirm_enabled=lambda:True,
            is_send_before_confirm_enabled=lambda:True,is_confirm_before_send_enabled=lambda:False,_check_account_execution=AsyncMock())
        handler=AutoDeliveryHandler(parent)
        parent.auto_delivery_handler=handler
        handler._ensure_order_amount_before_delivery=AsyncMock(return_value=True)
        handler.auto_confirm=AsyncMock(return_value={'success':True})
        with patch('common.db.session.async_session_maker',self.sessions),patch('app.services.shipping.legacy_bridge.async_session_maker',self.sessions),patch('common.db.compat.db_manager.get_item_info',return_value={'item_id':'item'}):
            for _ in range(2):
                await handler._handle_auto_delivery(parent.ws,{},'买家','buyer','item','chat','fixture',override_order_id='same',pre_check_result={'action':'allow'})
        self.assertEqual(parent.send_msg.call_count,2)
        handler.auto_confirm.assert_awaited_once()
        handler._ensure_order_amount_before_delivery.assert_awaited_once()
        async with self.sessions() as s:
            rows=(await s.scalars(select(DeliveryIntent))).all()
            self.assertEqual(len(rows),1);self.assertEqual(rows[0].content_state,'confirmed')

    async def test_internal_confirm_only_never_sends_or_consumes(self):
        import httpx
        from fastapi import FastAPI
        from app.services.shipping import commerce_routes
        from app.api.deps import require_internal_auth
        from common.services.delivery_execution import DeliveryExecution
        from common.models.card import Card
        service=DeliveryExecution(self.sessions)
        intent=await service.reserve(1,'a','same',1)
        await service.execute(intent.id,1,send=AsyncMock(return_value='confirmed'),confirm=None)
        handler=SimpleNamespace(auto_confirm=AsyncMock(return_value={'success':True}),is_auto_confirm_enabled=lambda:True,
                                send_msg=AsyncMock(),send_image_msg=AsyncMock())
        live=SimpleNamespace(ws=object(),auto_delivery_handler=handler,_check_account_execution=AsyncMock())
        app=FastAPI();app.include_router(commerce_routes.router);app.dependency_overrides[require_internal_auth]=lambda:None
        with patch.object(commerce_routes,'async_session_maker',self.sessions),patch('app.services.xianyu.cookie_manager.get_manager',return_value=SimpleNamespace(instances={'a':live})):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                response=await client.post('/internal/orders/fulfillment',json={'owner_id':1,'intent_id':intent.id,'confirm_only':True})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['data']['confirm_state'],'confirmed')
        handler.send_msg.assert_not_called();handler.send_image_msg.assert_not_called()
        async with self.sessions() as session:self.assertEqual((await session.get(Card,1)).data_content,'C')

if __name__=='__main__':unittest.main()
