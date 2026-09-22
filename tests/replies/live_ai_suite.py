"""S1→S2: replies真实选择链 + AI真实生成链 + 本地六协议HTTP端点。"""
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ai'))
import unittest
from unittest.mock import AsyncMock, patch
from sqlalchemy import insert, delete
import test_live_path
from test_protocols import BODIES
from common.services.reply_state import ReplyState
from common.models.reply_state import TABLES, metadata, reply_policies
from app.services.xianyu.auto_reply_service import AutoReplyService
from app.services.xianyu import ai_reply_engine
from types import SimpleNamespace


class RepliesLiveAITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_live_path.LivePathTests()
        await self.fixture.asyncSetUp()
        async with self.fixture.api.engine.begin() as conn:
            await conn.run_sync(lambda sync: metadata.create_all(sync, tables=TABLES))
        self.sessions = test_live_path.async_sessionmaker(self.fixture.api.engine, expire_on_commit=False)
        self.store = ReplyState(self.sessions)
        self.reply = AutoReplyService('account-a', SimpleNamespace(myid='seller'))
        self.reply.reply_state = self.store
        self.reply.get_keyword_reply = AsyncMock(return_value=None)
        self.reply.get_default_reply = AsyncMock(return_value='默认兜底')

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_six_protocols_two_strategies_with_shared_history(self):
        for provider, body in BODIES.items():
            await self.fixture.save(provider)
            self.fixture.server.body = body
            for strategy in ('legacy', 'ai_first'):
                with self.subTest(provider=provider, strategy=strategy):
                    async with self.sessions() as session:
                        await session.execute(delete(reply_policies))
                        await session.execute(insert(reply_policies).values(account_id='account-a', strategy=strategy))
                        await session.commit()
                    chat = provider + '-' + strategy
                    await self.store.record_message('account-a', chat, 'manual', 'assistant', '人工承诺明天发货', origin='manual')
                    await self.store.record_message('account-a', chat, 'buyer', 'user', '还有库存吗')
                    calls = len(self.fixture.server.calls)
                    with patch('app.services.xianyu.auto_reply_service.async_session_maker', self.sessions), \
                         patch.object(ai_reply_engine, 'get_ai_reply_engine', return_value=self.fixture.ai):
                        text = await self.reply.get_reply('buyer', 'buyer', '还有库存吗', chat)
                    if strategy == 'legacy':
                        self.assertEqual(text, '默认兜底')
                        self.assertEqual(len(self.fixture.server.calls), calls)
                    else:
                        self.assertEqual(text, '第一行\n第二行')
                        self.assertEqual(len(self.fixture.server.calls), calls + 1)
                        payload = self.fixture.server.calls[-1][2]
                        self.assertIn('人工承诺明天发货', str(payload))
                        self.assertIn('还有库存吗', str(payload))

    async def test_six_protocol_error_matrix_falls_back_with_explicit_error_decision(self):
        cases = [(401, {}, 'authentication', 0, None), (500, {}, 'http', 0, None),
                 (200, {'error': {'code': 'invalid_request', 'message': 'fixture'}}, 'provider', 0, None),
                 (200, {}, 'parse', 0, None), (200, {}, 'parse', 0, b'not-json'),
                 (200, {}, 'timeout', .65, None), (200, None, 'empty', 0, None)]
        for provider in BODIES:
            await self.fixture.save(provider)
            for strategy in ('ai_first', 'legacy'):
                async with self.sessions() as session:
                    await session.execute(delete(reply_policies))
                    await session.execute(insert(reply_policies).values(account_id='account-a', strategy=strategy))
                    await session.commit()
                for index, (status, body, stage, delay, raw) in enumerate(cases):
                    with self.subTest(provider=provider, strategy=strategy, stage=stage, case=index):
                        self.fixture.server.status = status
                        self.fixture.server.body = body if body is not None else json.loads(json.dumps(BODIES[provider], ensure_ascii=False).replace('第一行\\n第二行', ''))
                        self.fixture.server.delay, self.fixture.server.raw_response = delay, raw
                        trace = {}
                        token = self.reply._reply_trace_var.set(trace)
                        calls = len(self.fixture.server.calls)
                        try:
                            with patch('app.services.xianyu.auto_reply_service.async_session_maker', self.sessions), \
                                 patch.object(ai_reply_engine, 'get_ai_reply_engine', return_value=self.fixture.ai):
                                text = await self.reply.get_reply('buyer', 'buyer', '还有库存吗', f'{provider}-{strategy}-{index}')
                        finally:
                            self.reply._reply_trace_var.reset(token)
                        self.assertEqual(text, '默认兜底')
                        if strategy == 'legacy':
                            self.assertEqual(len(self.fixture.server.calls), calls)
                        else:
                            self.assertEqual(len(self.fixture.server.calls), calls + 1)
                            decisions = trace.get('context_snapshot', {}).get('reply_decisions', [])
                            self.assertIn({'source':'ai', 'kind':'error'}, decisions)
                            self.assertEqual(trace['context_snapshot']['ai_diagnostics']['stage'], stage)

if __name__ == '__main__': unittest.main()
