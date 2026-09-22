"""订单真实消息与历史同步行为；未调用平台。"""
import unittest
from types import SimpleNamespace
from sqlalchemy import BigInteger,select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from common.models.xy_order import XYOrder
from common.models.xy_account import XYAccount
from common.services.order_service import OrderService

@compiles(BigInteger,'sqlite')
def bigint(*args,**kw): return 'INTEGER'

class OrderIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine=create_async_engine('sqlite+aiosqlite:///:memory:')
        self.sessions=async_sessionmaker(self.engine,expire_on_commit=False)
        async with self.engine.begin() as c:
            for m in (XYOrder,XYAccount): await c.run_sync(m.__table__.create)
        async with self.sessions() as s:
            s.add_all([XYAccount(id=i,owner_id=i,account_id=f'a{i}',cookie='fixture',unb=str(i),login_method='cookie',status='active') for i in (1,2)])
            s.add_all([XYOrder(id=i,owner_id=i,account_id=f'a{i}',order_no='same',status='refunding') for i in (1,2)])
            await s.commit()
    async def asyncTearDown(self): await self.engine.dispose()

    async def test_message_does_not_touch_other_account_or_rewind_refund(self):
        async with self.sessions() as s:
            await OrderService(s).create_order_from_message('same','a2','pending_ship',buyer_id='buyer2')
        async with self.sessions() as s:
            a,b=await s.get(XYOrder,1),await s.get(XYOrder,2)
            self.assertIsNone(a.buyer_id)
            self.assertEqual(b.buyer_id,'buyer2')
            self.assertEqual(b.status,'refunding')

    async def test_import_retains_refund_and_marks_conflicting_evidence(self):
        async with self.sessions() as s:
            service=OrderService(s)
            await service._upsert_order({'order_no':'same','status':'pending_ship'},SimpleNamespace(owner_id=2,account_id='a2'))
        async with self.sessions() as s:
            row=await s.get(XYOrder,2)
            self.assertEqual(row.status,'refunding')
            self.assertTrue(row.metadata_json['status_conflict'])

    async def test_bare_ambiguous_order_lookup_fails_closed(self):
        async with self.sessions() as s:
            with self.assertRaises(ValueError): await OrderService(s).get_order_by_id('same')
            row=await OrderService(s).get_order_by_id('same',owner_id=2,account_id='a2')
            self.assertEqual(row.id,2)

    async def test_legacy_unscoped_mutations_do_not_touch_same_named_orders(self):
        async with self.sessions() as s:
            service=OrderService(s)
            self.assertFalse(await service.update_order_status('same','shipped'))
            self.assertFalse(await service.update_order_chat_id('same','chat'))
            self.assertFalse(await service.record_card_only_delivery('same','auto','CARD'))
        async with self.sessions() as s:
            self.assertEqual((await s.get(XYOrder,1)).status,'refunding')
            self.assertFalse((await s.get(XYOrder,2)).card_only_delivered)

    async def test_stale_preloaded_order_does_not_erase_new_refund_evidence(self):
        async with self.sessions() as s:
            stale=await s.get(XYOrder,2);s.expunge(stale);stale.status='pending_ship'
            await OrderService(s)._upsert_order({'order_no':'same','status':'shipped'},SimpleNamespace(owner_id=2,account_id='a2'),existing=stale)
        async with self.sessions() as s:
            self.assertEqual((await s.get(XYOrder,2)).status,'refunding')

    async def test_order_log_lookup_scopes_account_identity(self):
        from common.models.auto_reply_message_log import XYAutoReplyMessageLog
        async with self.engine.begin() as c:await c.run_sync(XYAutoReplyMessageLog.__table__.create)
        async with self.sessions() as s:
            s.add_all([XYAutoReplyMessageLog(id=i,owner_id=i,account_id=f'a{i}',chat_id='chat',sender_user_id='buyer',order_no='same',reply_strategy='auto_delivery',send_status='success' if i==1 else 'failed',send_fail_reason=None if i==1 else 'OTHER-ACCOUNT') for i in (1,2)])
            await s.commit()
            rows=await OrderService(s).get_delivery_log_status_by_identity([(1,'a1','same')])
            self.assertEqual(rows[(1,'a1','same')]['send_status'],'success')
            self.assertNotIn('OTHER-ACCOUNT',str(rows))

    async def test_status_checker_cancel_is_scoped_to_account(self):
        from unittest.mock import patch
        from common.services.order_service import OrderStatusChecker
        with patch('common.db.session.async_session_maker',self.sessions):
            await OrderStatusChecker('fixture','a2')._update_order_status_to_cancelled('same')
        async with self.sessions() as s:
            self.assertEqual((await s.get(XYOrder,1)).status,'refunding')
