"""S1 付款入口 -> 规则 -> 真实库存/意图 -> S2回执。"""
from unittest.mock import AsyncMock
import unittest
import test_fulfillment as fixtures
from common.models.card_item_relation import CardItemRelation
from common.services.order_delivery_runtime import OrderDeliveryRuntime
from common.models.xy_order import XYOrder
from common.models.card import Card

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.FulfillmentTests.asyncSetUp(self)
        async with self.engine.begin() as c: await c.run_sync(CardItemRelation.__table__.create)
        async with self.sessions() as s:
            card=await s.get(Card,1);card.item_id='item'
            order=await s.get(XYOrder,1);order.buyer_id='buyer';order.chat_id='chat'
            await s.commit()
        self.runtime=OrderDeliveryRuntime(self.sessions)
    async def asyncTearDown(self): await self.engine.dispose()
    async def test_payment_entry_double_event_uses_persistent_facts(self):
        text=AsyncMock(return_value={'delivery_state':'confirmed'});image=AsyncMock();confirm=AsyncMock(return_value='confirmed')
        for _ in range(2):
            result=await self.runtime.handle(account_id='a',order_no='same',item_id='item',buyer_id='buyer',chat_id='chat',send_text=text,send_image=image,confirm=confirm,mode='send_first')
            self.assertTrue(result)
        self.assertEqual(text.call_count,2);self.assertEqual(confirm.call_count,1)
    async def test_payment_entry_rejects_wrong_chat(self):
        text=AsyncMock();confirm=AsyncMock()
        await self.runtime.handle(account_id='a',order_no='same',item_id='item',buyer_id='buyer',chat_id='wrong-chat',send_text=text,send_image=text,confirm=confirm,mode='send_first')
        text.assert_not_called();confirm.assert_not_called()
    async def test_missing_sku_does_not_fall_back_to_generic_stock(self):
        async with self.sessions() as s:
            s.add(Card(id=3,user_id=1,item_id='item',name='blue',type='data',data_content='BLUE',is_multi_spec=True,spec_name='颜色',spec_value='蓝'));await s.commit()
        rules=await self.runtime.preview(1,'a','same')
        self.assertFalse(any(r['matched'] for r in rules))
