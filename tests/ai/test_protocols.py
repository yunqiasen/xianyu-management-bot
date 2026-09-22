"""S2：独立本地 HTTP 端点；不接触真实模型或闲鱼。"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec = importlib.util.spec_from_file_location('ai_gateway_fixture', Path(__file__).resolve().parents[2] / 'common/services/ai_gateway.py')
gateway = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gateway
spec.loader.exec_module(gateway)

BODIES = {
    'openai_compatible': {'choices': [{'message': {'content': '第一行\n第二行'}}]},
    'responses': {'status': 'completed', 'output': [{'type': 'reasoning', 'summary': []}, {'type': 'message', 'content': [{'type': 'output_text', 'text': '第一行\n第二行'}]}]},
    'azure': {'choices': [{'message': {'content': '第一行\n第二行'}}]},
    'anthropic': {'content': [{'type': 'thinking', 'thinking': 'hidden'}, {'type': 'text', 'text': '第一行\n第二行'}]},
    'gemini': {'candidates': [{'content': {'parts': [{'text': 'hidden', 'thought': True}, {'text': '第一行\n第二行'}]}}]},
    'dashscope_app': {'output': {'text': '第一行\n第二行'}},
}
MESSAGES = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'earlier'}, {'role': 'assistant', 'content': 'promise'}, {'role': 'user', 'content': 'latest'}]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((self.path, dict(self.headers), body))
        delay = getattr(self.server, "delay_by_model", {}).get(body.get("model"), self.server.delay)
        if delay:
            time.sleep(delay)
        self.send_response(self.server.status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        try:
            self.wfile.write(getattr(self.server, 'raw_response', None) or json.dumps(self.server.body).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.calls = []
        self.server.status = 200
        self.server.delay = 0
        self.server.raw_response = None
        self.server.delay_by_model = {}
        self.assertTrue(hasattr(gateway, 'generate_text'), '统一入口尚未实现生成请求')

    def settings(self, provider):
        return dict(provider_type=provider, base_url=self.base, api_key='fixture-secret', model_name='model', app_id='app', azure_deployment='deploy', azure_api_version='2024-10-21', timeout_seconds=.5)

    async def test_six_protocols_have_distinct_payload_and_auth(self):
        for provider, response in BODIES.items():
            with self.subTest(provider=provider):
                self.server.body = response
                result = await gateway.generate_text(self.settings(provider), MESSAGES)
                self.assertEqual(result.text, '第一行\n第二行')
                self.assertEqual(result.stage, 'success')
                path, headers, body = self.server.calls[-1]
                self.assertFalse(body.get('stream', False))
                if provider == 'responses':
                    self.assertIn('input', body)
                    self.assertNotIn('messages', body)
                    self.assertEqual(body['max_output_tokens'], 512)
                elif provider == 'azure':
                    self.assertEqual(headers['api-key'], 'fixture-secret')
                    self.assertIn('api-version=2024-10-21', path)
                elif provider == 'anthropic':
                    self.assertEqual(headers['x-api-key'], 'fixture-secret')
                    self.assertEqual(body['system'], 'system')
                    self.assertEqual(body['messages'][1]['content'], 'promise')
                elif provider == 'gemini':
                    self.assertEqual(headers['x-goog-api-key'], 'fixture-secret')
                    self.assertEqual(body['contents'][1]['role'], 'model')
                    self.assertEqual(body['contents'][1]['parts'][0]['text'], 'promise')
                elif provider == 'dashscope_app':
                    self.assertIn('/apps/app/', path)
                    self.assertIn('promise', json.dumps(body['input']))
                    self.assertNotIn('model', body)
                else:
                    self.assertEqual(headers['Authorization'], 'Bearer fixture-secret')
                    self.assertEqual(body['messages'], MESSAGES)
        self.assertEqual(len(self.server.calls), 6)

    async def test_error_matrix_no_retry_and_no_secret_echo(self):
        for provider in BODIES:
            for status, body, expected in [
                (401, {'error': {'message': 'fixture-secret', 'code': 'invalid_api_key'}}, 'authentication'),
                (429, {'error': {'message': 'fixture-secret', 'code': 'rate_limit'}}, 'http'),
                (200, {'error': {'message': 'fixture-secret'}}, 'provider'),
                (200, [], 'parse'),
                (200, {}, 'parse'),
            ]:
                with self.subTest(provider=provider, status=status, body=body):
                    self.server.status, self.server.body = status, body
                    before = len(self.server.calls)
                    with self.assertRaises(gateway.AIGatewayError) as caught:
                        await gateway.generate_text(self.settings(provider), MESSAGES)
                    self.assertEqual(caught.exception.stage, expected)
                    self.assertNotIn('fixture-secret', str(caught.exception))
                    self.assertEqual(len(self.server.calls), before + 1)

    async def test_empty_response_per_protocol(self):
        for provider, response in BODIES.items():
            with self.subTest(provider=provider):
                self.server.body = json.loads(json.dumps(response, ensure_ascii=False).replace('第一行\\n第二行', ''))
                with self.assertRaises(gateway.AIGatewayError) as caught:
                    await gateway.generate_text(self.settings(provider), MESSAGES)
                self.assertEqual(caught.exception.stage, 'empty')

    async def test_timeout_and_event_loop_remains_responsive(self):
        for provider in BODIES:
            with self.subTest(provider=provider):
                self.server.body = BODIES[provider]
                self.server.delay = .15
                settings = self.settings(provider) | {'timeout_seconds': .04}
                ticked = asyncio.Event()
                async def tick():
                    await asyncio.sleep(.01)
                    ticked.set()
                tick_task = asyncio.create_task(tick())
                with self.assertRaises(gateway.AIGatewayError) as caught:
                    await gateway.generate_text(settings, MESSAGES)
                self.assertEqual(caught.exception.stage, 'timeout')
                self.assertTrue(ticked.is_set())
                await tick_task

    async def test_context_budget_keeps_latest_message(self):
        self.server.body = BODIES['openai_compatible']
        settings = self.settings('openai_compatible') | {'max_context_chars': 256}
        result = await gateway.generate_text(settings, [MESSAGES[0], {'role': 'user', 'content': 'old' * 100}, MESSAGES[-1]])
        self.assertGreater(result.trimmed_messages, 0)
        self.assertEqual(self.server.calls[-1][2]['messages'][-1]['content'], 'latest')

    async def test_full_urls_preserve_each_protocol_and_auth(self):
        paths = {'openai_compatible': '/v1/chat/completions', 'responses': '/v1/responses', 'azure': '/openai/deployments/deploy/chat/completions?api-version=2024-10-21', 'anthropic': '/v1/messages', 'gemini': '/v1beta/models/model:generateContent', 'dashscope_app': '/api/v1/apps/app/completion'}
        for provider, path in paths.items():
            with self.subTest(provider=provider):
                self.server.body = BODIES[provider]
                settings = self.settings(provider) | {'base_url': self.base + path}
                if provider == 'azure':
                    settings['azure_auth_mode'] = 'bearer'
                await gateway.generate_text(settings, MESSAGES)
                request_path, headers, _ = self.server.calls[-1]
                self.assertEqual(request_path, path)
                if provider == 'azure':
                    self.assertEqual(headers['Authorization'], 'Bearer fixture-secret')
                    self.assertNotIn('api-key', headers)

    async def test_non_json_response_is_parse_error_without_body_echo(self):
        self.server.raw_response = b'<html>fixture-secret</html>'
        for provider in BODIES:
            with self.subTest(provider=provider), self.assertRaises(gateway.AIGatewayError) as caught:
                await gateway.generate_text(self.settings(provider), MESSAGES)
            self.assertEqual(caught.exception.stage, 'parse')
            self.assertNotIn('fixture-secret', str(caught.exception))

    async def test_slow_model_does_not_block_independent_request(self):
        self.server.body = BODIES['openai_compatible']
        self.server.delay_by_model = {'slow': .2}
        slow = asyncio.create_task(gateway.generate_text(self.settings('openai_compatible') | {'model_name': 'slow'}, MESSAGES))
        while not self.server.calls:
            await asyncio.sleep(.005)
        fast = await gateway.generate_text(self.settings('openai_compatible') | {'model_name': 'fast'}, MESSAGES)
        self.assertEqual(fast.stage, 'success')
        self.assertFalse(slow.done())
        self.assertEqual((await slow).stage, 'success')
        self.assertEqual(len(self.server.calls), 2)
