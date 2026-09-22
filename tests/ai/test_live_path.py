"""S1→S2：真实 API 保存/测试与真实 generate_reply，对照独立 HTTP 接收端。"""
import asyncio
import importlib.util
import json
import sys
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import async_sessionmaker
import test_api
from test_protocols import Handler, BODIES
from test_settings import ROOT
from common.models.ai_chat_message import AIChatMessage
from common.services.ai_provider_service import test_ai_connection
import app.services

app.services.__path__.append(str(ROOT / 'websocket/app/services'))
spec = importlib.util.spec_from_file_location('live_ai_engine_fixture', ROOT / 'websocket/app/services/xianyu/ai_reply_engine.py')
engine_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine_module)

# SQLite 自增列须为 INTEGER；仅测试编译适配，生产 MySQL 模型不变。
@compiles(BigInteger, 'sqlite')
def _sqlite_bigint(type_, compiler, **kw):
    return 'INTEGER'


class LivePathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = test_api.APITests()
        await self.api.asyncSetUp()
        async with self.api.engine.begin() as conn:
            await conn.run_sync(AIChatMessage.__table__.create)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.calls = []
        self.server.status, self.server.delay = 200, 0
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.ai = engine_module.AIReplyEngine()
        self.persistence = patch.object(engine_module, 'async_session_maker', async_sessionmaker(self.api.engine, expire_on_commit=False))
        self.persistence.start()

    async def asyncTearDown(self):
        self.persistence.stop()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        await self.api.asyncTearDown()

    async def save(self, provider):
        settings = dict(provider_type=provider, base_url=self.base, api_key='fixture-secret', model_name='model', app_id='app', azure_deployment='deploy', azure_api_version='2024-10-21', ai_enabled=True, timeout_seconds=.5, max_tokens=200)
        response = await self.api.client.put('/api/v1/ai-reply-settings/account-a', json=settings)
        self.assertTrue(response.json()['success'], response.text)

    async def test_saved_protocols_work_in_test_button_and_real_engine(self):
        for provider, body in BODIES.items():
            with self.subTest(provider=provider):
                await self.save(provider)
                self.server.body = body
                button = await self.api.client.post('/api/v1/ai-reply-test/account-a')
                self.assertTrue(button.json()['success'], button.text)
                self.assertEqual(button.json()['data']['stage'], 'success')
                text = await self.ai.generate_reply('还有库存吗', {'title': 'fixture', 'price': 10}, 'chat-' + provider, 'account-a', 'buyer', 'item', self.api.session, skip_wait=True)
                self.assertEqual(text, '第一行\n第二行')
                test_request, live_request = self.server.calls[-2:]
                self.assertEqual(test_request[0], live_request[0])
                self.assertEqual(test_request[1], live_request[1] | {'Content-Length': test_request[1]['Content-Length']})
                # 用线上实际消息再次通过配置测试兼容入口，整个请求体须完全相同。
                settings = self.api.account.metadata_json['ai_reply_settings']
                messages = [{'role': 'system', 'content': 'same'}, {'role': 'user', 'content': 'same'}]
                await test_ai_connection(provider, self.base, 'fixture-secret', 'model', settings=settings, messages=messages)
                await self.ai._call_ai_api(settings, messages)
                self.assertEqual(self.server.calls[-2], self.server.calls[-1])
                rows = (await self.api.session.execute(select(AIChatMessage).where(AIChatMessage.chat_id == 'chat-' + provider))).scalars().all()
                self.assertEqual([row.role for row in rows], ['user', 'assistant'])

    async def test_test_button_distinguishes_failure_stages(self):
        await self.save('responses')
        for status, body, stage in [(401, {'error': {'message': 'fixture-secret'}}, 'authentication'), (200, {}, 'parse'), (200, {'output': []}, 'empty')]:
            with self.subTest(stage=stage):
                self.server.status, self.server.body = status, body
                result = await self.api.client.post('/api/v1/ai-reply-test/account-a')
                self.assertFalse(result.json()['success'])
                self.assertEqual(result.json()['data']['stage'], stage)
                self.assertNotIn('fixture-secret', result.text)

    async def test_lock_isolated_across_accounts(self):
        first = await self.ai._get_chat_lock(('account-a', 'same-chat'))
        second = await self.ai._get_chat_lock(('account-b', 'same-chat'))
        self.assertIsNot(first, second)

    async def test_plain_prompt_and_shared_manual_history_reach_model(self):
        await self.save('openai_compatible')
        await self.api.client.put('/api/v1/ai-reply-settings/account-a', json={'custom_prompts': '自定义客服语气'})
        self.server.body = BODIES['openai_compatible']
        self.assertIn('history', __import__('inspect').signature(self.ai.generate_reply).parameters, 'AI 尚未接入连续历史契约')
        history = [{'account_id': 'account-a', 'chat_id': 'shared', 'role': 'assistant', 'content': '人工已承诺明天发货', 'status': 'confirmed'}]
        text = await self.ai.generate_reply('还有库存吗', {}, 'shared', 'account-a', 'buyer', 'item', self.api.session, skip_wait=True, history=history)
        self.assertIsNotNone(text)
        messages = self.server.calls[-1][2]['messages']
        self.assertEqual(messages[0]['content'], '自定义客服语气')
        self.assertIn('人工已承诺明天发货', messages[1]['content'])

    async def test_shared_history_rejects_another_account(self):
        await self.save('openai_compatible')
        self.server.body = BODIES['openai_compatible']
        self.assertIn('history', __import__('inspect').signature(self.ai.generate_reply).parameters, 'AI 尚未接入连续历史契约')
        history = [{'account_id': 'other', 'chat_id': 'shared', 'role': 'assistant', 'content': 'private'}]
        text = await self.ai.generate_reply('还有库存吗', {}, 'shared', 'account-a', 'buyer', 'item', self.api.session, skip_wait=True, history=history)
        self.assertIsNone(text)
        self.assertEqual(self.server.calls, [])

    async def test_changed_config_discards_inflight_model_output(self):
        await self.save('openai_compatible')
        self.server.body = BODIES['openai_compatible']
        self.server.delay = .15
        task = asyncio.create_task(self.ai.generate_reply('还有库存吗', {}, 'stale', 'account-a', 'buyer', 'item', self.api.session, skip_wait=True))
        while not self.server.calls:
            await asyncio.sleep(.005)
        async with async_sessionmaker(self.api.engine, expire_on_commit=False)() as session:
            from app.services.ai_reply_service import AIReplySettingsService
            from common.models.xy_account import XYAccount
            account = await session.get(XYAccount, 1)
            await AIReplySettingsService(session).update_settings(account, {'max_tokens': 300})
        self.assertIsNone(await task, '旧配置生成的回复应停止，不写入待发送正文')

    async def test_reply_diagnostics_preserve_error_stage(self):
        await self.save('responses')
        self.server.body = {'error': {'code': 'invalid_api_key', 'message': 'fixture-secret'}}
        self.server.status = 401
        self.assertIn('diagnostics', __import__('inspect').signature(self.ai.generate_reply).parameters)
        diagnostics = {}
        text = await self.ai.generate_reply('还有库存吗', {}, 'error-trace', 'account-a', 'buyer', 'item', self.api.session, skip_wait=True, diagnostics=diagnostics)
        self.assertIsNone(text)
        self.assertEqual(diagnostics['stage'], 'authentication')
        self.assertEqual(diagnostics['status_code'], 401)
        self.assertTrue(diagnostics['call_id'])
        self.assertNotIn('fixture-secret', json.dumps(diagnostics))

    async def test_completed_model_then_manual_pause_reports_skip(self):
        await self.save('openai_compatible')
        self.server.body = BODIES['openai_compatible']
        diagnostics = {}
        with patch.object(engine_module.pause_manager, 'is_chat_paused', return_value=True):
            text = await self.ai.generate_reply('还有库存吗', {}, 'paused', 'account-a', 'buyer', 'item', self.api.session, skip_wait=True, diagnostics=diagnostics)
        self.assertIsNone(text)
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(diagnostics['stage'], 'skipped')
        self.assertEqual(diagnostics['reason'], 'manual_pause')
