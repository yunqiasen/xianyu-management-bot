"""续跑 S1: 发送意图核实、连续历史与逐段接管。"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import test_state
from sqlalchemy import select


class FollowupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_state.ReplyStateTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await test_state.ReplyStateTests.asyncTearDown(self)

    async def test_unknown_reconcile_requires_exact_platform_identity_and_survives_restart(self):
        transport = AsyncMock(return_value={'status': 'unknown', 'messageId': 'platform-mid'})
        await self.store.send('a', 'c', 'r', 'program text', transport, origin='program')
        store = self.mod.ReplyState(self.sessions)
        self.assertTrue(hasattr(store, 'reconcile'), '缺少持久出站核实入口')
        with self.assertRaises(ValueError):
            await store.reconcile('a', 'c', 'r', {'status':'confirmed', 'messageId':'different'})
        with self.assertRaises(ValueError):
            await store.reconcile('b', 'c', 'r', {'status':'confirmed', 'messageId':'platform-mid'})
        result = await store.reconcile('a', 'c', 'r', {'status':'confirmed', 'messageId':'platform-mid',
            'account_id':'a', 'chat_id':'c', 'source':'platform_history'})
        self.assertEqual(result['status'], 'confirmed')
        await store.send('a', 'c', 'r', 'program text', transport, origin='program')
        transport.assert_awaited_once()
        self.assertEqual((await store.history('a', 'c'))[0]['status'], 'confirmed')

    async def test_auto_partial_takeover_preserves_only_sent_segment_in_outbox(self):
        from test_runtime import RuntimeTests
        runtime = RuntimeTests()
        runtime.sessions, runtime.engine, runtime.mod, runtime.store = self.sessions, self.engine, self.mod, self.store
        from app.services.xianyu.auto_reply_service import AutoReplyService
        from types import SimpleNamespace
        service = AutoReplyService('a', SimpleNamespace(myid='seller'))
        service.reply_state = self.store
        async def send(**kwargs):
            await self.store.pause('a', 'c', 10)
            return {'success': True, 'mid': 'mid-1'}
        service.xianyu_instance.send_msg = AsyncMock(side_effect=send)
        token = service._reply_trace_var.set({'source_message_id':'e1', 'source_message':'hi', 'reply_strategy':'ai'})
        try:
            await service._send_text_with_separator(None, 'c', 'buyer', 'first######second######third')
        finally:
            service._reply_trace_var.reset(token)
        self.assertEqual(service.xianyu_instance.send_msg.await_count, 1, '接管后仍发送后续段')
        async with self.sessions() as session:
            rows = (await session.execute(select(self.mod.reply_outbox))).mappings().all()
        self.assertEqual(len(rows), 1, '自动回复尚未接入持久出站链')
        self.assertEqual([r['content'] for r in await self.store.history('a', 'c')], ['first'])

    async def test_intent_and_history_are_atomic_before_external_send(self):
        from sqlalchemy import event
        send = AsyncMock(return_value={'status':'confirmed'})
        def fail_history(conn, cursor, statement, *args):
            if statement.startswith('INSERT INTO xy_reply_events'):
                raise RuntimeError('fixture crash before history insert')
        event.listen(self.engine.sync_engine, 'before_cursor_execute', fail_history)
        try:
            with self.assertRaises(RuntimeError):
                await self.store.send('a', 'c', 'atomic', 'hello', send, pause_minutes=10)
        finally:
            event.remove(self.engine.sync_engine, 'before_cursor_execute', fail_history)
        send.assert_not_awaited()
        async with self.sessions() as session:
            rows = (await session.execute(select(self.mod.reply_outbox))).all()
        self.assertEqual(rows, [], '历史失败留下无法恢复的已提交意图')
        self.assertEqual(await self.store.pause_remaining('a', 'c'), 0)
        await self.store.send('a', 'c', 'atomic', 'hello', send, pause_minutes=10)
        send.assert_awaited_once()

    async def test_internal_dispatch_joins_outbox_history_and_keeps_wire_identity(self):
        from types import SimpleNamespace
        self.assertTrue(hasattr(self.store, 'dispatch_message'), '内部程序发送缺少统一意图适配')
        request = SimpleNamespace(command='send_text_message', owner_id=1, account_id='a', request_id='dispatch-1',
            generation=5, credential_version=2, config_version=3,
            payload={'cid':'c', 'to_user_id':'buyer', 'text':'程序消息', 'origin':'program', 'item_id':''})
        transport = AsyncMock(return_value=SimpleNamespace(status='unknown', result={'mid':'wire-mid', 'uuid':'wire-uuid'}, error_code='ack_lost'))
        first = await self.store.dispatch_message(request, transport)
        second = await self.store.dispatch_message(request, transport)
        self.assertEqual(first['status'], 'unknown'); self.assertEqual(second['status'], 'unknown')
        self.assertEqual(first['uuid'], 'wire-uuid')
        row = await self.store.outbound('a', 'c', 'dispatch-1')
        self.assertEqual(row['result']['correlation']['generation'], 5)
        self.assertEqual((await self.store.history('a','c'))[0]['origin'], 'program')
        self.assertEqual(await self.store.pause_remaining('a','c'), 0)
        transport.assert_awaited_once()

    async def test_backfilled_history_keeps_chronological_windows(self):
        await self.store.record_message('a', 'c', 'new', 'user', '最新买家', occurred_at=300)
        await self.store.record_message('a', 'c', 'old', 'user', '补拉旧消息', occurred_at=100)
        await self.store.record_message('a', 'c', 'manual', 'assistant', '人工承诺', origin='manual', occurred_at=200)
        page = await self.store.history('a', 'c', limit=2)
        self.assertEqual([r['content'] for r in page], ['人工承诺', '最新买家'])
        previous = await self.store.history('a', 'c', limit=2, before=page[0]['cursor'])
        self.assertEqual([r['content'] for r in previous], ['补拉旧消息'])

    async def test_platform_echo_reuses_outbound_and_does_not_create_manual_takeover(self):
        send = AsyncMock(return_value={'status':'unknown', 'messageId':'echo-mid'})
        await self.store.send('a','c','request','机器人正文',send,origin='auto',sender_id='seller')
        _, fresh = await self.store.record_message('a','c','echo-mid','assistant','机器人正文','seller',origin='manual')
        self.assertFalse(fresh, '已有关联出站的回声被误记为新人工消息')
        self.assertEqual(len(await self.store.history('a','c')), 1)
        self.assertEqual((await self.store.outbound('a','c','request'))['status'], 'confirmed')

    async def test_multi_part_confirmation_retains_each_part_identity(self):
        from app.services.xianyu.auto_reply_service import AutoReplyService
        from types import SimpleNamespace
        service = AutoReplyService('a', SimpleNamespace(myid='seller'))
        service.reply_state = self.store
        mids = iter(['one', 'two'])
        async def send(**kwargs):
            mid = next(mids)
            future = asyncio.get_running_loop().create_future()
            future.set_result({'code':200, 'body':{'messageId':'platform-' + mid}})
            return {'success':True, 'mid':mid, 'send_future':future}
        service.xianyu_instance.send_msg = send
        service.xianyu_instance.wait_send_reject_reason = AsyncMock(return_value=None)
        token = service._reply_trace_var.set({'source_message_id':'e', 'reply_strategy':'ai'})
        try:
            results = await service._send_text_with_separator(None,'c','buyer','one######two')
            await service._writeback_send_status(None, [(r['send_future'],r['mid'],r['outbox_request_id'],'c') for r in results])
        finally: service._reply_trace_var.reset(token)
        rows = [await self.store.outbound('a','c',r['outbox_request_id']) for r in results]
        self.assertEqual([row['status'] for row in rows], ['confirmed','confirmed'])
        self.assertEqual([row['result']['messageId'] for row in rows], ['platform-one','platform-two'])
        self.assertEqual([row['content'] for row in await self.store.history('a','c')], ['one','two'])
