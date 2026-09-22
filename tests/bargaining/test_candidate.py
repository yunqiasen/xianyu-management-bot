"""DEV45 S1 API/正式回复选择链 → S2 独立 HTTP；全部使用临时数据。"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from sqlalchemy import delete, select, func

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/ai'))
import test_live_path as live
from common.core.config import get_settings
from common.models.reply_state import TABLES, metadata, reply_events
from common.models.ai_chat_message import AIChatMessage
from common.services.reply_state import ReplyState
from app.services.xianyu.auto_reply_service import AutoReplyService
from app.services.xianyu import ai_reply_engine


class CandidateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = live.LivePathTests()
        await self.fixture.asyncSetUp()
        self.sessions = live.async_sessionmaker(self.fixture.api.engine, expire_on_commit=False)
        async with self.fixture.api.engine.begin() as conn:
            await conn.run_sync(lambda sync: metadata.create_all(sync, tables=TABLES))
        # 模型存在后按正式迁移契约建表；关闭路径的回归故意不依赖此表。
        try:
            from common.models.bargaining_event import BargainingEvent
        except ImportError:
            pass
        else:
            async with self.fixture.api.engine.begin() as conn:
                await conn.run_sync(lambda sync: BargainingEvent.__table__.create(sync, checkfirst=True))
        self.store = ReplyState(self.sessions)
        self.reply = AutoReplyService('account-a', SimpleNamespace(myid='seller'))
        self.reply.reply_state = self.store
        self.reply.get_keyword_reply = AsyncMock(return_value=None)
        self.reply.get_default_reply = AsyncMock(return_value='默认兜底')
        self.patches = [
            patch.object(get_settings(), 'xymb_enable_bargaining_v2', True),
            patch('app.services.xianyu.auto_reply_service.async_session_maker', self.sessions),
            patch.object(ai_reply_engine, 'get_ai_reply_engine', return_value=self.fixture.ai),
        ]
        for p in self.patches:
            p.start()
        await self.fixture.save('openai_compatible')
        self.fixture.server.body = live.BODIES['openai_compatible']
        await self.configure(max_bargain_rounds=50)

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        await self.fixture.asyncTearDown()

    async def configure(self, **kwargs):
        result = await self.fixture.api.client.put('/api/v1/ai-reply-settings/account-a', json=kwargs)
        self.assertTrue(result.json()['success'], result.text)

    async def inbound(self, text, event='buyer-1', chat='chat'):
        return await self.store.record_message('account-a', chat, event, 'user', text, sender_id='buyer')

    async def generate(self, text, chat='chat'):
        trace = {}
        token = self.reply._reply_trace_var.set(trace)
        try:
            result = await self.reply.get_reply('buyer', 'buyer', text, chat)
        finally:
            self.reply._reply_trace_var.reset(token)
        return result, trace.get('context_snapshot', {}).get('ai_diagnostics', {})

    async def test_intent_counter_distinguishes_inquiry_and_negotiation(self):
        cases = [('评价怎么样', 'default', 0), ('剃须刀怎么用', 'tech', 0),
                 ('刀具参数和型号', 'tech', 0), ('多少钱', 'price', 0),
                 ('这个价格含邮费吗', 'price', 0), ('100元是价格吗', 'price', 0),
                 ('能便宜20元吗', 'price', 1), ('80元卖吗', 'price', 1)]
        for i, (text, intent, expected) in enumerate(cases):
            with self.subTest(text=text):
                chat = f'case-{i}'
                await self.inbound(text, chat=chat)
                result, diag = await self.generate(text, chat)
                self.assertEqual(result, '第一行\n第二行')
                self.assertEqual(diag.get('bargaining', {}).get('count'), expected)
                self.assertEqual(diag['bargaining']['intent'], intent)

    async def test_platform_event_dedup_survives_history_cleanup_and_restart(self):
        from common.models.bargaining_event import BargainingEvent
        text = '能便宜20元吗'
        await self.inbound(text)
        _, first = await self.generate(text)
        _, duplicate = await self.generate(text)
        self.assertEqual(first['bargaining']['count'], 1)
        self.assertEqual(duplicate['bargaining']['count'], 1)
        async with self.sessions() as session:
            await session.execute(delete(AIChatMessage))
            await session.execute(delete(reply_events))
            await session.commit()
        self.fixture.ai = live.engine_module.AIReplyEngine()
        # 同一身份重新出现不增加；新平台事件即使同内容也增加。
        await self.inbound(text)
        history = await self.store.history('account-a', 'chat')
        diag = {}
        await self.fixture.ai.generate_reply(text, {}, 'chat', 'account-a', 'buyer', '', self.fixture.api.session,
                                             skip_wait=True, history=history, diagnostics=diag)
        self.assertEqual(diag['bargaining']['count'], 1)
        await self.inbound(text, 'buyer-2')
        diag = {}
        await self.fixture.ai.generate_reply(text, {}, 'chat', 'account-a', 'buyer', '', self.fixture.api.session,
                                             skip_wait=True, history=await self.store.history('account-a', 'chat'), diagnostics=diag)
        self.assertEqual(diag['bargaining']['count'], 2)
        async with self.sessions() as session:
            self.assertEqual((await session.execute(select(func.count()).select_from(BargainingEvent))).scalar(), 2)

    async def test_recent_complete_turn_manual_promise_and_trim_diagnostics(self):
        await self.configure(max_context_chars=2400)
        for i in range(30):
            await self.inbound(f'旧问题{i}' + '长' * 90, f'user-{i}')
            await self.store.record_message('account-a', 'chat', f'manual-{i}', 'assistant',
                f'人工承诺{i}' + '长' * 90, origin='manual')
        await self.inbound('最新问题', 'newest')
        result, diag = await self.generate('最新问题')
        self.assertEqual(result, '第一行\n第二行')
        payload = self.fixture.server.calls[-1][2]['messages']
        text = str(payload)
        self.assertIn('人工承诺29', text)
        self.assertIn('旧问题29', text)
        self.assertIn('最新问题', text)
        self.assertNotIn('旧问题0', text)
        window = diag['history_window']
        self.assertGreater(window['trimmed_messages'], 0)
        self.assertGreater(window['manual_messages'], 0)
        self.assertEqual(window['last_event_id'], 'newest')
        self.assertLessEqual(sum(len(m['content']) for m in payload), 2400)
        self.assertEqual(diag['trimmed_messages'], window['trimmed_messages'])

    async def test_pending_manual_and_seller_bargaining_never_count(self):
        await self.store.record_message('account-a', 'chat', 'pending', 'assistant', '未确认承诺便宜50元', origin='manual', status='unknown')
        await self.store.record_message('account-a', 'chat', 'seller', 'assistant', '给你便宜20元', origin='manual')
        await self.inbound('多少钱')
        _, diag = await self.generate('多少钱')
        self.assertEqual(diag['bargaining']['count'], 0)
        prompt = str(self.fixture.server.calls[-1][2])
        self.assertNotIn('未确认承诺', prompt)
        self.assertIn('给你便宜20元', prompt)

    async def test_inquiry_does_not_hit_cap_custom_prompts_and_zero_limits(self):
        await self.configure(max_bargain_rounds=1, max_discount_percent=0, max_discount_amount=0,
            custom_prompts='{"default":"普通提示","tech":"技术提示","price":"价格提示"}')
        await self.inbound('便宜点吧')
        capped, diag = await self.generate('便宜点吧')
        self.assertTrue(capped)
        self.assertEqual(self.fixture.server.calls, [])
        self.assertEqual(diag.get('reason'), 'bargain_limit')
        await self.inbound('价格是多少', 'inquiry')
        result, diag = await self.generate('价格是多少')
        self.assertEqual(result, '第一行\n第二行')
        payload = self.fixture.server.calls[-1][2]['messages']
        self.assertEqual(payload[0]['content'], '价格提示')
        self.assertIn('最大优惠百分比：0%', payload[1]['content'])
        self.assertIn('最大优惠金额：0元', payload[1]['content'])

    async def test_count_reconciles_shared_events_outside_prompt_window(self):
        await self.configure(max_bargain_rounds=500)
        for i in range(105):
            await self.inbound('能便宜20元吗', f'old-{i}')
        await self.inbound('多少钱', 'newest-inquiry')
        _, diag = await self.generate('多少钱')
        self.assertEqual(diag['bargaining']['count'], 105)

    async def test_diagnostics_api_reads_real_generated_trace_and_hides_secrets(self):
        from common.models.auto_reply_message_log import XYAutoReplyMessageLog
        async with self.fixture.api.engine.begin() as conn:
            await conn.run_sync(XYAutoReplyMessageLog.__table__.create)
        await self.inbound('能便宜20元吗')
        _, diag = await self.generate('能便宜20元吗')
        async with self.sessions() as session:
            session.add(XYAutoReplyMessageLog(account_id='account-a', owner_id=7, chat_id='chat', sender_user_id='buyer',
                context_snapshot={'ai_diagnostics': {**diag, 'api_key': 'fixture-secret'}, 'cookie': 'private-cookie'}))
            await session.commit()
        response = await self.fixture.api.client.get('/api/v1/ai-reply-settings/account-a/bargaining-diagnostics')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('fixture-secret', response.text)
        self.assertNotIn('private-cookie', response.text)
        data = response.json()['data']
        self.assertTrue(data['candidate_enabled'])
        self.assertEqual(data['records'][0]['bargaining']['count'], 1)
        self.assertEqual(data['records'][0]['history_window']['last_event_id'], 'buyer-1')
        denied = await self.fixture.api.client.get('/api/v1/ai-reply-settings/other/bargaining-diagnostics')
        self.assertEqual(denied.status_code, 404)
        with patch.object(get_settings(), 'xymb_enable_bargaining_v2', False):
            async with self.fixture.api.engine.begin() as conn:
                await conn.run_sync(XYAutoReplyMessageLog.__table__.drop)
            response = await self.fixture.api.client.get('/api/v1/ai-reply-settings/account-a/bargaining-diagnostics')
            self.assertEqual(response.json()['data'], {'candidate_enabled': False, 'records': []})

    async def test_raw_platform_event_reaches_real_ai_once(self):
        from app.services.xianyu.message_handler import MessageHandler
        with patch.object(MessageHandler, '_load_message_expire_time', return_value=3600):
            handler = MessageHandler('account-a', 'seller')
        handler.reply_state = self.store
        diagnostics = []
        async def callback(parsed, websocket):
            _, diag = await self.generate(parsed['send_message'], parsed['chat_id'])
            diagnostics.append(diag)
        handler.set_chat_message_handler(callback)
        raw = {'1': {'2': 'chat@goofish', '5': 1000, '10': {
            'reminderContent': '能便宜20元吗', 'senderUserId': 'buyer',
            'bizTag': '{"messageId":"native-platform-1"}'}}}
        self.assertTrue(await handler._process_single_message(raw, None))
        self.assertTrue(await handler._process_single_message(raw, None))
        self.assertEqual(len(self.fixture.server.calls), 1)
        self.assertEqual(diagnostics[0]['bargaining']['count'], 1)
        self.assertEqual(diagnostics[0]['history_window']['last_event_id'], 'native-platform-1')

    async def test_candidate_storage_error_is_diagnosed_without_legacy_count(self):
        from common.models.bargaining_event import BargainingEvent
        async with self.fixture.api.engine.begin() as conn:
            await conn.run_sync(BargainingEvent.__table__.drop)
        await self.inbound('能便宜20元吗')
        _, diag = await self.generate('能便宜20元吗')
        self.assertEqual(self.fixture.server.calls, [])
        self.assertEqual(diag.get('stage'), 'persistence')
        self.assertEqual(diag.get('reason'), 'bargaining_events')

    async def test_oversize_current_turn_reports_budget_without_call(self):
        await self.configure(max_context_chars=1000)
        text = '能便宜20元吗' + '长' * 1500
        await self.inbound(text)
        _, diag = await self.generate(text)
        self.assertEqual(self.fixture.server.calls, [])
        self.assertEqual(diag['stage'], 'budget')
        self.assertEqual(diag['bargaining']['count'], 1)
        self.assertEqual(diag['history_window']['reason'], 'latest_turn_over_budget')

    async def test_disabled_preserves_legacy_single_character_detection_and_window(self):
        with patch.object(get_settings(), 'xymb_enable_bargaining_v2', False):
            from common.models.bargaining_event import BargainingEvent
            async with self.fixture.api.engine.begin() as conn:
                await conn.run_sync(BargainingEvent.__table__.drop)
            self.assertEqual(self.fixture.ai.detect_intent('剃须刀怎么用', 'account-a'), 'price')
            for i in range(12):
                await self.store.record_message('account-a', 'chat', f'm{i}', 'assistant', f'原消息{i}', origin='manual')
            await self.inbound('多少钱')
            result, diag = await self.generate('多少钱')
            self.assertEqual(result, '第一行\n第二行')
            self.assertNotIn('bargaining', diag)
            prompt = str(self.fixture.server.calls[-1][2])
            self.assertIn('当前议价次数：1', prompt)
            self.assertNotIn('原消息0', prompt)
            self.assertIn('原消息11', prompt)


if __name__ == '__main__':
    unittest.main()
