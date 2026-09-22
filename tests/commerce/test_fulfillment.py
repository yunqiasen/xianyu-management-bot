"""S1 真实事务履约；S2 回执/供应商仅使用本地桩。"""
import asyncio
import unittest
from unittest.mock import AsyncMock
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from common.models.card import Card
from common.models.delivery_rule import DeliveryRule
from common.models.xy_order import XYOrder
from common.models.xy_account import XYAccount
from common.models.delivery_intent import DeliveryIntent
from common.services.delivery_execution import DeliveryExecution

@compiles(BigInteger, 'sqlite')
def bigint(*args, **kw): return 'INTEGER'
@compiles(LONGTEXT, 'sqlite')
def longtext(*args, **kw): return 'TEXT'

class FulfillmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as c:
            for m in (Card, XYOrder, XYAccount, DeliveryIntent, DeliveryRule):
                await c.run_sync(m.__table__.create)
        async with self.sessions() as s:
            s.add_all([XYAccount(id=1,owner_id=1,account_id='a',unb='1',cookie='fixture',login_method='cookie',status='active'),
                       Card(id=1,user_id=1,name='卡',type='data',data_content='A\nB\nC'),
                       XYOrder(id=1,owner_id=1,account_id='a',order_no='same',item_id='item',status='pending_ship',quantity=2),
                       XYOrder(id=2,owner_id=1,account_id='a',order_no='second',item_id='item',status='pending_ship',quantity=2)])
            await s.commit()
        self.service = DeliveryExecution(self.sessions)
        self.send = AsyncMock(return_value='confirmed')
        self.confirm = AsyncMock(return_value='confirmed')

    async def asyncTearDown(self): await self.engine.dispose()

    async def reserve(self, **kw):
        return await self.service.reserve(1,'a','same',1, **kw)

    async def test_duplicate_payment_reserves_once_and_sends_exact_quantity(self):
        first = await self.reserve()
        second = await self.reserve()
        self.assertEqual(first.id,second.id)
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm)
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm)
        self.assertEqual(self.send.call_count,1)
        self.assertEqual(self.send.call_args.args[0], {'texts':['A','B'],'images':[]})
        async with self.sessions() as s:
            self.assertEqual((await s.get(Card,1)).data_content,'C')
            self.assertEqual((await s.get(DeliveryIntent,first.id)).content_state,'confirmed')

    async def test_insufficient_inventory_rolls_back_all(self):
        first=await self.reserve()
        with self.assertRaisesRegex(ValueError,'库存不足'):
            await self.service.reserve(1,'a','second',1)
        async with self.sessions() as s:
            self.assertEqual((await s.get(Card,1)).data_content,'C')
        self.assertIsNotNone(first)

    async def test_unknown_send_holds_stock_and_never_repeats(self):
        first=await self.reserve()
        self.send.side_effect=TimeoutError('SECRET')
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm)
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm)
        self.assertEqual(self.send.call_count,1); self.confirm.assert_not_called()
        async with self.sessions() as s:
            row=await s.get(DeliveryIntent,first.id)
            self.assertEqual(row.content_state,'unknown'); self.assertNotIn('SECRET',str(row.last_error))
            self.assertEqual((await s.get(Card,1)).data_content,'C')

    async def test_explicit_not_sent_releases_inventory(self):
        first=await self.reserve(); self.send.return_value='not_sent'
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm)
        async with self.sessions() as s:
            self.assertEqual((await s.get(Card,1)).data_content,'A\nB\nC')
            self.assertEqual((await s.get(DeliveryIntent,first.id)).content_state,'not_sent')

    async def test_crash_at_send_boundary_requires_verification(self):
        first=await self.reserve(); self.send.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.execute(first.id,1,send=self.send,confirm=self.confirm)
        self.send.reset_mock(side_effect=True)
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm)
        self.send.assert_not_called(); self.confirm.assert_not_called()

    async def test_confirm_only_after_restart_does_not_send_or_consume(self):
        first=await self.reserve(); self.confirm.return_value='not_sent'
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm,now=100)
        fresh=DeliveryExecution(self.sessions); self.confirm.return_value='confirmed'
        await fresh.execute(first.id,1,send=self.send,confirm=self.confirm,now=161)
        self.assertEqual(self.send.call_count,1); self.assertEqual(self.confirm.call_count,2)
        async with self.sessions() as s:
            self.assertEqual((await s.get(Card,1)).data_content,'C')
            self.assertEqual((await s.get(DeliveryIntent,first.id)).confirm_state,'confirmed')

    async def test_confirmation_has_three_persistent_retries_without_resending_content(self):
        first = await self.reserve()
        self.confirm.return_value = 'not_sent'
        # Initial attempt plus 60/300/900-second retries; restart between attempts.
        for attempt, (at, next_at) in enumerate(((100, 160), (160, 460), (460, 1360), (1360, 0)), 1):
            service = DeliveryExecution(self.sessions)
            row = await service.execute(first.id, 1, send=self.send, confirm=self.confirm, now=at)
            self.assertEqual(row.confirm_attempts, attempt)
            self.assertEqual(row.next_retry_at, next_at)
            self.assertEqual(self.confirm.call_count, attempt)
            if next_at:
                await service.execute(first.id, 1, send=self.send, confirm=self.confirm, now=next_at - 1)
                self.assertEqual(self.confirm.call_count, attempt)
        row = await DeliveryExecution(self.sessions).execute(first.id, 1, send=self.send, confirm=self.confirm, now=9999)
        self.assertEqual(row.confirm_state, 'pending')
        self.assertEqual(row.last_error, 'confirmation_retry_exhausted')
        self.assertEqual(self.confirm.call_count, 4)
        self.assertEqual(self.send.call_count, 1)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Card, 1)).data_content, 'C')

    async def test_unknown_confirmation_must_query_before_retry(self):
        first=await self.reserve(); self.confirm.side_effect=TimeoutError()
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm,now=100)
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm,now=200)
        self.assertEqual(self.confirm.call_count,1)
        await self.service.reconcile(first.id,1,'confirm','confirmed',evidence='fixture-receipt')
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm,now=300)
        self.assertEqual(self.send.call_count,1); self.assertEqual(self.confirm.call_count,1)

    async def test_card_only_and_confirm_first_are_separate_facts(self):
        first=await self.reserve(mode='card_only')
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm)
        self.confirm.assert_not_called()
        async with self.sessions() as s:
            row=await s.get(DeliveryIntent,first.id)
            self.assertEqual(row.confirm_state,'not_required')
            self.assertEqual((await s.get(XYOrder,1)).status,'pending_ship')

    async def test_owner_account_sku_and_quantity_guards(self):
        with self.assertRaises(PermissionError): await self.service.reserve(2,'a','same',1)
        with self.assertRaises(PermissionError): await self.service.reserve(1,'other','same',1)
        async with self.sessions() as s:
            card=await s.get(Card,1); card.is_multi_spec=True; card.spec_name='颜色'; card.spec_value='蓝'
            await s.commit()
        with self.assertRaisesRegex(ValueError,'规格'): await self.reserve()
        async with self.sessions() as s:
            order=await s.get(XYOrder,1); order.spec_name='颜色'; order.spec_value='蓝'; order.quantity=0
            await s.commit()
        with self.assertRaisesRegex(ValueError,'数量'): await self.reserve()

    async def test_api_timeout_is_single_purchase_and_persistent(self):
        async with self.sessions() as s:
            card=await s.get(Card,1); card.type='api'; card.api_config='{"url":"http://fixture"}'
            await s.commit()
        first=await self.reserve(); purchase=AsyncMock(side_effect=TimeoutError('TOKEN'))
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm,purchase=purchase)
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm,purchase=purchase)
        self.assertEqual(purchase.call_count,1); self.send.assert_not_called()
        self.assertEqual(purchase.call_args.kwargs['idempotency_key'],first.id)

    async def test_image_payload_and_permission(self):
        async with self.sessions() as s:
            card=await s.get(Card,1); card.type='image'; card.image_url='/static/fixture.png'
            await s.commit()
        first=await self.reserve()
        with self.assertRaises(PermissionError): await self.service.execute(first.id,2,send=self.send,confirm=self.confirm)
        await self.service.execute(first.id,1,send=self.send,confirm=self.confirm)
        self.assertEqual(self.send.call_args.args[0]['images'],['/static/fixture.png'])

    async def test_generation_change_stops_old_writeback(self):
        first=await self.reserve()
        async def send(payload):
            async with self.sessions() as s:
                account=await s.get(XYAccount,1); account.metadata_json={'xy_runtime':{'generation':1}}
                await s.commit()
            return 'confirmed'
        await self.service.execute(first.id,1,send=send,confirm=self.confirm)
        self.confirm.assert_not_called()
        async with self.sessions() as s:
            self.assertEqual((await s.get(DeliveryIntent,first.id)).content_state,'sending')
