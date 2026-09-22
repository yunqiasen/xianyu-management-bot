import unittest
from unittest.mock import AsyncMock
import test_fulfillment as fixtures
from common.models.delivery_intent import DeliveryIntent
from common.models.card import Card

class ExtraBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self): await fixtures.FulfillmentTests.asyncSetUp(self)
    async def asyncTearDown(self): await self.engine.dispose()
    async def test_manual_resend_is_distinct_and_requires_confirmed_old_content(self):
        old=await self.service.reserve(1,'a','same',1)
        with self.assertRaises(ValueError): await self.service.resend(old.id,1,request_id='fixture-new',acknowledged=True,reason='fixture resend')
        await self.service.execute(old.id,1,send=self.send,confirm=self.confirm)
        async with self.sessions() as s:
            card=await s.get(Card,1);card.data_content='C\nD';await s.commit()
        with self.assertRaises(ValueError): await self.service.resend(old.id,1,request_id='fixture-new',acknowledged=False,reason='fixture resend')
        new=await self.service.resend(old.id,1,request_id='fixture-new',acknowledged=True,reason='fixture resend')
        repeated=await self.service.resend(old.id,1,request_id='fixture-new',acknowledged=True,reason='fixture resend')
        self.assertEqual(new.id,repeated.id);self.assertNotEqual(new.id,old.id)
        await self.service.execute(new.id,1,send=self.send,confirm=self.confirm)
        self.assertEqual(self.send.call_count,2)
        async with self.sessions() as s: self.assertEqual((await s.get(DeliveryIntent,old.id)).content_state,'confirmed')

    async def test_retry_budget_and_cooldown_leave_todo(self):
        old=await self.service.reserve(1,'a','same',1)
        self.confirm.return_value='not_sent'
        # Irregular scheduler ticks still obey the persisted 60/300/900-second
        # waits and the initial attempt plus three retries from I12.
        for now, attempts in ((100,1),(101,1),(159,1),(161,2),(222,2),
                              (500,3),(1399,3),(1400,4),(9999,4)):
            result = await self.service.execute(old.id,1,send=self.send,confirm=self.confirm,now=now)
            self.assertEqual(self.confirm.call_count, attempts)
            self.assertEqual(result.confirm_attempts, attempts)
        self.assertEqual(self.send.call_count,1)
        async with self.sessions() as s:
            row = await s.get(DeliveryIntent,old.id)
            self.assertEqual(row.confirm_state,'pending')
            self.assertEqual(row.last_error,'confirmation_retry_exhausted')
            self.assertEqual(row.next_retry_at,0)

    async def test_confirm_first_crash_recovers_content_without_repeat_confirm(self):
        old=await self.service.reserve(1,'a','same',1,mode='confirm_first')
        self.send.side_effect=TimeoutError()
        await self.service.execute(old.id,1,send=self.send,confirm=self.confirm)
        async with self.sessions() as s:
            row=await s.get(DeliveryIntent,old.id)
            self.assertEqual(row.confirm_state,'confirmed');self.assertEqual(row.content_state,'unknown')
        await self.service.execute(old.id,1,send=self.send,confirm=self.confirm)
        self.assertEqual(self.send.call_count,1);self.assertEqual(self.confirm.call_count,1)

    async def test_lease_lost_after_submission_prevents_old_writeback(self):
        old=await self.service.reserve(1,'a','same',1)
        lost=False
        async def check():
            if lost: raise RuntimeError('lease lost')
        async def send(payload):
            nonlocal lost
            lost=True
            return 'confirmed'
        await self.service.execute(old.id,1,send=send,confirm=self.confirm,check=check)
        self.confirm.assert_not_called()
        async with self.sessions() as s:self.assertEqual((await s.get(DeliveryIntent,old.id)).content_state,'sending')

    async def test_new_lease_resumes_only_pending_confirmation_after_restart(self):
        from common.models.xy_account import XYAccount
        old=await self.service.reserve(1,'a','same',1)
        self.confirm.return_value='not_sent'
        await self.service.execute(old.id,1,send=self.send,confirm=self.confirm,now=100)
        async with self.sessions() as s:
            account=await s.get(XYAccount,1);account.metadata_json={'xy_runtime':{'generation':1}};await s.commit()
        self.confirm.return_value='confirmed'
        await self.service.execute(old.id,1,send=self.send,confirm=self.confirm,now=161,check=AsyncMock())
        self.assertEqual(self.confirm.call_count,2);self.assertEqual(self.send.call_count,1)

    async def test_purchase_completed_before_crash_never_purchases_again(self):
        async with self.sessions() as s:
            card=await s.get(Card,1);card.type='api';card.api_config='{"url":"http://fixture"}';await s.commit()
        old=await self.service.reserve(1,'a','same',1)
        purchase=AsyncMock(return_value={'texts':['A','B'],'images':[]})
        await self.service.execute(old.id,1,send=None,confirm=None,purchase=purchase)
        await self.service.execute(old.id,1,send=self.send,confirm=self.confirm,purchase=purchase)
        self.assertEqual(purchase.call_count,1)

    async def test_api_uses_reserved_configuration_after_card_edit(self):
        async with self.sessions() as s:
            card=await s.get(Card,1);card.type='api';card.api_config='{"url":"http://original"}';await s.commit()
        old=await self.service.reserve(1,'a','same',1)
        async with self.sessions() as s:
            card=await s.get(Card,1);card.api_config='{"url":"http://changed"}';await s.commit()
        purchase=AsyncMock(return_value={'texts':['A','B'],'images':[]})
        await self.service.execute(old.id,1,send=self.send,confirm=self.confirm,purchase=purchase)
        self.assertIn('http://original',purchase.call_args.args[0])

    async def test_unknown_supplier_is_queried_not_purchased_again(self):
        async with self.sessions() as s:
            card=await s.get(Card,1);card.type='api';card.api_config='{"url":"http://buy","query_url":"http://query"}';await s.commit()
        old=await self.service.reserve(1,'a','same',1)
        purchase=AsyncMock(side_effect=TimeoutError())
        await self.service.execute(old.id,1,send=self.send,confirm=self.confirm,purchase=purchase)
        query=AsyncMock(return_value={'texts':['A','B'],'images':[]})
        await self.service.verify_supplier(old.id,1,query=query,check=AsyncMock())
        await self.service.execute(old.id,1,send=self.send,confirm=self.confirm,purchase=purchase)
        self.assertEqual(purchase.call_count,1);self.assertEqual(query.call_count,1)
        self.assertEqual(query.call_args.kwargs['idempotency_key'],old.id)
        self.assertEqual(self.send.call_count,1)

    async def test_refund_arriving_after_reservation_stops_external_delivery(self):
        from common.models.xy_order import XYOrder
        old=await self.service.reserve(1,'a','same',1)
        async with self.sessions() as s:
            order=await s.get(XYOrder,1);order.status='refunded';await s.commit()
        await self.service.execute(old.id,1,send=self.send,confirm=self.confirm)
        self.send.assert_not_called();self.confirm.assert_not_called()

    async def test_confirmed_quantity_updates_card_counter_exactly_once(self):
        row=await self.service.reserve(1,'a','same',1)
        for _ in range(2):await self.service.execute(row.id,1,send=self.send,confirm=self.confirm)
        async with self.sessions() as s:self.assertEqual((await s.get(Card,1)).delivery_count,2)

    async def test_resend_requires_reason_and_keeps_actor(self):
        old=await self.service.reserve(1,'a','same',1)
        await self.service.execute(old.id,1,send=self.send,confirm=self.confirm)
        async with self.sessions() as s:
            card=await s.get(Card,1);card.data_content='C\nD';await s.commit()
        new=await self.service.resend(old.id,1,request_id='reason-fixture',acknowledged=True,reason='客户确认需要重发')
        self.assertEqual(new.evidence[0]['actor_id'],1)
        self.assertEqual(new.evidence[0]['reason'],'客户确认需要重发')
