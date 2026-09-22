"""S1: 现有自动回复和消息分发入口回归。"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'websocket'))
from app.services.xianyu.auto_reply_service import AutoReplyService
from app.services.xianyu.message_handler import MessageHandler
import test_state


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_state.ReplyStateTests.asyncSetUp(self)
        self.service = AutoReplyService('a', SimpleNamespace(myid='seller'))
        self.service.reply_state = self.store
        self.service.get_keyword_reply = AsyncMock(return_value=None)
        self.service.get_ai_reply = AsyncMock(return_value='AI')
        self.service.get_default_reply = AsyncMock(return_value='默认')

    async def asyncTearDown(self):
        await test_state.ReplyStateTests.asyncTearDown(self)

    async def test_existing_get_reply_uses_persisted_legacy_strategy(self):
        from sqlalchemy import insert
        async with self.sessions() as session:
            await session.execute(insert(self.mod.reply_policies).values(account_id='a', strategy='legacy'))
            await session.commit()
        with patch('app.services.xianyu.auto_reply_service.async_session_maker', self.sessions):
            result = await self.service.get_reply('buyer', 'buyer', 'hi', 'c')
        self.assertEqual(result, '默认')
        self.service.get_ai_reply.assert_not_awaited()

    async def test_empty_keyword_skips_ai(self):
        self.service.get_keyword_reply.return_value = 'EMPTY_REPLY'
        with patch('app.services.xianyu.auto_reply_service.async_session_maker', self.sessions):
            self.assertIsNone(await self.service.get_reply('buyer', 'buyer', 'hi', 'c'))
        self.service.get_ai_reply.assert_not_awaited()

    async def test_exclusive_before_keyword_and_default(self):
        from sqlalchemy import insert
        async with self.sessions() as session:
            await session.execute(insert(self.mod.exclusive_replies).values(id='rule', account_id='a', item_id='item', content='专属', image_url='', enabled=True))
            await session.commit()
        with patch('app.services.xianyu.auto_reply_service.async_session_maker', self.sessions):
            result = await self.service.get_reply('buyer', 'buyer', 'hi', 'c', 'item')
        self.assertEqual(result, '专属')
        self.service.get_keyword_reply.assert_not_awaited()

    async def test_manual_pause_keeps_history(self):
        await self.store.pause('a', 'c', 10)
        with patch('app.services.xianyu.auto_reply_service.async_session_maker', self.sessions):
            self.assertIsNone(await self.service.get_reply('buyer', 'buyer', 'hi', 'c'))
        self.service.get_ai_reply.assert_not_awaited()

    async def test_timeout_receipt_never_written_as_success(self):
        future = asyncio.get_running_loop().create_future()
        self.service.xianyu_instance.wait_send_reject_reason = AsyncMock(return_value=None)
        self.service.auto_reply_log_service.safe_update_send_status = AsyncMock()
        await self.service._writeback_send_status(1, [(future, 'mid')])
        self.service.auto_reply_log_service.safe_update_send_status.assert_awaited_with(1, 'unknown', None)

    async def test_debounce_retains_originals_and_latest_task(self):
        with patch.object(MessageHandler, '_load_message_expire_time', return_value=3600):
            handler = MessageHandler('a', 'seller')
        handler.message_debounce_delay = 0.03
        callback = AsyncMock()
        await handler.schedule_debounced_reply('c', {'send_message': 'one', 'event_id': 'e1'}, callback)
        await handler.schedule_debounced_reply('c', {'send_message': 'two', 'event_id': 'e2'}, callback)
        await asyncio.sleep(0.06)
        callback.assert_awaited_once()
        merged = callback.await_args.args[0]
        self.assertEqual(merged['send_message'], 'one\ntwo')
        self.assertEqual(merged['source_event_ids'], ['e1', 'e2'])

    async def test_handle_chat_sends_default_once_only_after_confirmed_receipt(self):
        from sqlalchemy import insert
        from common.models.default_reply import DefaultReply, DefaultReplyRecord
        from common.models.xy_personal_blacklist import XYPersonalBlacklist
        from common.models.xy_platform_blacklist import XYPlatformBlacklist
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda sync: self.mod.metadata.create_all(sync, tables=[DefaultReply.__table__, DefaultReplyRecord.__table__, XYPersonalBlacklist.__table__, XYPlatformBlacklist.__table__]))
        async with self.sessions() as session:
            await session.execute(insert(self.mod.reply_policies).values(account_id='a', strategy='legacy'))
            await session.execute(insert(DefaultReply.__table__).values(account_id='a', enabled=True, reply_content='默认正文', reply_once=True, reply_type='text'))
            await session.commit()
        self.service.get_default_reply = AutoReplyService.get_default_reply.__get__(self.service)
        self.service._get_account = AsyncMock(return_value=SimpleNamespace(id=1, owner_id=1, pause_duration=0))
        self.service.get_filter_keywords = AsyncMock(return_value=[])
        self.service._load_reply_delay = AsyncMock(return_value=0)
        self.service._check_chat_processed = AsyncMock(return_value=False)
        self.service._mark_chat_processed = AsyncMock()
        self.service._send_notification = AsyncMock()
        self.service._record_auto_reply_log = AsyncMock(return_value=7)
        self.service.auto_reply_log_service.safe_update_send_status = AsyncMock()
        future = asyncio.get_running_loop().create_future()
        future.set_result({'code': 200, 'body': {'messageId': 'sent'}})
        self.service.xianyu_instance.send_msg = AsyncMock(return_value={'success': True, 'send_future': future, 'mid': 'mid'})
        self.service.xianyu_instance.wait_send_reject_reason = AsyncMock(return_value=None)
        tasks = []
        self.service.xianyu_instance._create_tracked_task = lambda coro: tasks.append(asyncio.create_task(coro))
        message = dict(send_user_id='buyer', send_user_name='buyer', send_message='hello', chat_id='c', event_id='e1')
        with patch('app.services.xianyu.auto_reply_service.async_session_maker', self.sessions):
            await self.service.handle_chat_message(message, None)
            await asyncio.gather(*tasks)
            await self.service.handle_chat_message({**message, 'event_id': 'e2'}, None)
        self.service.xianyu_instance.send_msg.assert_awaited_once()
        self.assertFalse(await self.store.reserve_once('a', 'c', '', 'third'))
        self.service.auto_reply_log_service.safe_update_send_status.assert_awaited_with(7, 'success', None)
        self.assertEqual([m['role'] for m in await self.store.history('a', 'c')], ['user', 'assistant', 'user'])

    async def test_message_dispatch_persists_raw_event_before_callback_and_dedups(self):
        with patch.object(MessageHandler, '_load_message_expire_time', return_value=3600):
            handler = MessageHandler('a', 'seller')
        handler.reply_state = self.store
        seen = []
        async def callback(parsed, websocket):
            rows = await self.store.history('a', 'c')
            self.assertEqual(rows[-1]['content'], 'hello')
            seen.append(parsed['event_id'])
        handler.set_chat_message_handler(callback)
        raw = {'1': {'2': 'c@goofish', '5': 1000, '10': {'reminderContent': 'hello', 'senderUserId': 'buyer', 'bizTag': '{"messageId":"event-1"}'}}}
        self.assertTrue(await handler._process_single_message(raw, None))
        self.assertTrue(await handler._process_single_message(raw, None))
        self.assertEqual(seen, ['event-1'])
        self.assertEqual(len(await self.store.history('a', 'c')), 1)

    async def test_slow_ai_manual_takeover_blocks_send_but_keeps_inbound(self):
        from common.models.xy_personal_blacklist import XYPersonalBlacklist
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda sync: self.mod.metadata.create_all(sync, tables=[XYPersonalBlacklist.__table__]))
        self.service._get_account = AsyncMock(return_value=SimpleNamespace(id=1, owner_id=1, pause_duration=10))
        self.service.get_filter_keywords = AsyncMock(return_value=[])
        self.service._load_reply_delay = AsyncMock(return_value=0)
        self.service._check_chat_processed = AsyncMock(return_value=False)
        self.service._mark_chat_processed = AsyncMock()
        self.service._record_auto_reply_log = AsyncMock(return_value=1)
        self.service._send_notification = AsyncMock()
        self.service.xianyu_instance.send_msg = AsyncMock()
        async def delayed_ai(*args):
            await self.store.pause('a', 'c', 10)
            return '生成的旧内容'
        self.service.get_ai_reply = delayed_ai
        with patch('app.services.xianyu.auto_reply_service.async_session_maker', self.sessions):
            await self.service.handle_chat_message(dict(send_user_id='buyer', send_message='hello', chat_id='c', event_id='e1'), None)
        self.service.xianyu_instance.send_msg.assert_not_awaited()
        self.assertEqual((await self.store.history('a', 'c'))[0]['content'], 'hello')
        self.assertEqual(self.service._record_auto_reply_log.await_args.args[0]['decision_reason'], 'chat_paused_after_delay')

    async def test_live_ai_entry_receives_shared_manual_and_latest_buyer_history(self):
        from app.services.xianyu import ai_reply_engine
        self.service._get_account = AsyncMock(return_value=SimpleNamespace(ai_reply_block_ordered_users=False))
        engine = SimpleNamespace(is_ai_enabled=AsyncMock(return_value=True), get_ai_settings=AsyncMock(return_value={}),
                                 _get_api_provider_name=lambda settings: 'fixture', generate_reply=AsyncMock(return_value='AI正文'))
        await self.store.record_message('a', 'c', 'm', 'assistant', '人工承诺明天发货', origin='manual')
        await self.store.record_message('a', 'c', 'b', 'user', '确认一下')
        with patch.object(ai_reply_engine, 'get_ai_reply_engine', return_value=engine):
            result = await AutoReplyService.get_ai_reply(self.service, None, 'buyer', 'buyer', '确认一下', None, 'c')
        self.assertEqual(result, 'AI正文')
        self.assertIn('history', engine.generate_reply.await_args.kwargs, '真实AI入口未传共享历史')
        rows = engine.generate_reply.await_args.kwargs['history']
        self.assertEqual([r['content'] for r in rows], ['人工承诺明天发货', '确认一下'])
