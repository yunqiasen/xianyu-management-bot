import unittest
from unittest.mock import AsyncMock
import test_fulfillment as fixtures
from common.models.card import Card
from common.models.delivery_intent import DeliveryIntent
from common.services.delivery_transport import send_payload

class FollowupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self): await fixtures.FulfillmentTests.asyncSetUp(self)
    async def asyncTearDown(self): await self.engine.dispose()
    async def test_per_piece_results_persist_even_partial_delivery(self):
        row=await self.service.reserve(1,'a','same',1)
        async def send(payload):
            return await send_payload(payload,AsyncMock(side_effect=[{'delivery_state':'confirmed'},{'delivery_state':'unknown'}]),AsyncMock(),
                record=lambda part,state:self.service.record_content(row.id,1,part,state))
        result=await self.service.execute(row.id,1,send=send,confirm=None)
        async with self.sessions() as s: result=await s.get(DeliveryIntent,row.id)
        pieces=[e for e in result.evidence or [] if e.get('phase')=='content_piece']
        self.assertEqual([p['result'] for p in pieces],['sending','confirmed','sending','unknown'])
        self.assertEqual(result.content_state,'unknown')
        async with self.sessions() as s:self.assertEqual((await s.get(Card,1)).data_content,'C')
    async def test_manual_supplier_evidence_unlocks_send_without_purchase(self):
        async with self.sessions() as s:
            card=await s.get(Card,1);card.type='api';card.api_config='{}';await s.commit()
        row=await self.service.reserve(1,'a','same',1)
        await self.service.execute(row.id,1,send=None,confirm=None,purchase=AsyncMock(side_effect=TimeoutError))
        method=getattr(self.service,'reconcile_purchase',None)
        self.assertIsNotNone(method,'缺少人工采购凭据闭环')
        await method(row.id,1,texts=['X','Y'],evidence='receipt:123')
        purchase=AsyncMock(side_effect=AssertionError('禁止重复采购'))
        result=await self.service.execute(row.id,1,send=AsyncMock(return_value='confirmed'),confirm=None,purchase=purchase)
        self.assertEqual(result.content_state,'confirmed');purchase.assert_not_called()
        self.assertEqual(result.payload['texts'],['X','Y'])
        self.assertEqual(result.evidence[-1]['actor_id'],1)
    async def test_confirm_first_failure_does_not_purchase_api_card(self):
        async with self.sessions() as s:
            card=await s.get(Card,1);card.type='api';card.api_config='{}';await s.commit()
        row=await self.service.reserve(1,'a','same',1,mode='confirm_first')
        purchase=AsyncMock(return_value={'texts':['X','Y']})
        await self.service.execute(row.id,1,send=None,confirm=AsyncMock(return_value='not_sent'),purchase=purchase)
        purchase.assert_not_called()

    async def test_confirm_not_sent_evidence_reopens_only_unknown_substep(self):
        row=await self.service.reserve(1,'a','same',1)
        await self.service.execute(row.id,1,send=AsyncMock(return_value='confirmed'),confirm=None)
        async def confirm(number):
            return await self.service.confirm_step(row.id,1,'confirm',AsyncMock(return_value={'success':False}))
        await self.service.execute(row.id,1,send=None,confirm=confirm)
        await self.service.reconcile(row.id,1,'confirm','not_sent',evidence='platform:absent')
        callback=AsyncMock(return_value={'success':True})
        result=await self.service.confirm_step(row.id,1,'confirm',callback)
        self.assertEqual(result,'confirmed');callback.assert_awaited_once()
    async def test_intent_never_moves_content_to_changed_buyer_or_chat(self):
        from common.models.xy_order import XYOrder
        async with self.sessions() as s:
            order=await s.get(XYOrder,1);order.buyer_id='original';order.chat_id='original-chat';await s.commit()
        row=await self.service.reserve(1,'a','same',1)
        async with self.sessions() as s:
            order=await s.get(XYOrder,1);order.buyer_id='changed';order.chat_id='changed-chat';await s.commit()
        send=AsyncMock(return_value='confirmed')
        await self.service.execute(row.id,1,send=send,confirm=None)
        send.assert_not_called()
