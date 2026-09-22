"""S2: real Xianyu socket/session transports against local HTTP and WebSocket peers."""
import asyncio
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from aiohttp import web
from websockets.server import serve
from websockets.client import connect
import test_flow as fixtures
from common.models.xy_account import XYAccount
from common.services import account_policy


class OutboundTests(unittest.IsolatedAsyncioTestCase):
    request = fixtures.FlowTests.request

    async def asyncSetUp(self):
        await fixtures.FlowTests.asyncSetUp(self)
        self.addAsyncCleanup(self.cleanup)
        from app.services.xianyu.xianyu_async import XianyuAsync
        self.live = object.__new__(XianyuAsync)
        self.live._account_runtime = self.runtime
        self.live._pending_mid_futures = {}
        self.live.cookie_id, self.live.myid, self.live.user_id = 'fixture', '101', 7
        self.live.cookies_str = 'unb=101; _m_h5_tk=offline_fixture;'
        self.live.proxy_config, self.live.session = {}, None
        self.wire_calls, self.http_calls = [], []
        self.live.connection_manager = SimpleNamespace(ws=None)
        self.manager.instances['fixture'] = self.live
        await self.set_interval(.15)
        async def peer(socket):
            async for wire in socket:
                packet = json.loads(wire)
                self.wire_calls.append((time.monotonic(), packet))
                await socket.send(json.dumps({'code':200, 'headers':packet.get('headers', {}),
                                              'body':{'messageId':'local-receipt'}}))
        self.server = await serve(peer, '127.0.0.1', 0)
        raw = await connect(f'ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}')
        self.addAsyncCleanup(raw.close)
        self.live.connection_manager.ws = self.live.fenced_socket(raw)
        async def receive():
            async for wire in raw:
                self.live._dispatch_mid_response(json.loads(wire))
        self.receiver = asyncio.create_task(receive())
        async def http(request):
            self.http_calls.append(time.monotonic())
            return web.json_response({'ok':True}, status=int(request.query.get('status','200')),
                headers={'Retry-After':request.query.get('retry_after','45')})
        app = web.Application(); app.router.add_post('/business', http)
        self.http_server = web.AppRunner(app); await self.http_server.setup()
        site = web.TCPSite(self.http_server, '127.0.0.1', 0); await site.start()
        self.http_url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/business'
        await self.live.create_session()

    async def cleanup(self):
        if getattr(self, 'live', None) and getattr(self.live, 'session', None):
            await self.live.close_session()
        if getattr(self, 'live', None) and self.live.ws:
            await self.live.ws.close()
        if hasattr(self, 'receiver'):
            await asyncio.gather(self.receiver, return_exceptions=True)
        if hasattr(self, 'server'):
            self.server.close(); await self.server.wait_closed()
        if hasattr(self, 'http_server'):
            await self.http_server.cleanup()
        await fixtures.FlowTests.asyncTearDown(self)

    async def set_interval(self, seconds):
        async with self.sessions() as session:
            account = await session.get(XYAccount, 1)
            state = account_policy.snapshot(account)
            state['config_values']['risk'] = {'min_interval_seconds':seconds}
            account_policy.store(account, state)
            await session.commit()

    async def test_legacy_http_and_text_send_share_one_account_budget(self):
        async with self.live.session.post(self.http_url) as response:
            self.assertEqual(response.status, 200)
        result = await self.live.send_msg(self.live.ws, 'chat', 'buyer', 'hello')
        self.assertTrue(result['success'], result)
        receipt = await asyncio.wait_for(result['send_future'], 1)
        self.assertEqual(receipt['body']['messageId'], 'local-receipt')
        self.assertEqual((len(self.http_calls), len(self.wire_calls)), (1, 1))
        self.assertGreaterEqual(self.wire_calls[0][0] - self.http_calls[0], .12)

    async def test_control_frame_does_not_mark_business_ready_or_leak_pending_futures(self):
        await self.runtime.record_connection(False)
        await self.live.ws.send(json.dumps({'lwp':'/reg', 'headers':{'mid':'registration'}, 'body':[]}))
        result = await self.live.send_msg(self.live.ws, 'chat', 'buyer', 'too early')
        self.assertFalse(result['success'])
        self.assertTrue(result['definitely_not_sent'])
        self.assertEqual(result['error_code'], 'account_not_ready')
        self.assertEqual(self.live._pending_mid_futures, {})
        self.assertEqual([packet['lwp'] for _, packet in self.wire_calls], ['/reg'])

    async def test_rpc_transport_spends_budget_once_not_twice(self):
        await self.set_interval(5)
        result = await asyncio.wait_for(self.dispatcher.execute(self.request()), 1)
        self.assertEqual(result.status, 'confirmed', result)
        self.assertEqual(len(self.wire_calls), 1)

    async def test_long_budget_wait_is_known_not_sent_and_cleans_mid(self):
        await self.budget.defer('fixture', 60)
        result = await self.live.send_msg(self.live.ws, 'chat', 'buyer', 'wait')
        self.assertFalse(result['success'])
        self.assertTrue(result['definitely_not_sent'])
        self.assertEqual(result['error_code'], 'budget_wait')
        self.assertEqual(self.live._pending_mid_futures, {})
        self.assertEqual(self.wire_calls, [])

    async def test_image_send_returns_real_receipt_future(self):
        self.live._get_image_size_from_url = AsyncMock(return_value=(4, 4))
        result = await self.live.send_image_msg(self.live.ws, 'chat', 'buyer', 'https://img.alicdn.com/fixture.png')
        self.assertTrue(result['success'], result)
        receipt = await asyncio.wait_for(result['send_future'], 1)
        self.assertEqual(receipt['body']['messageId'], 'local-receipt')
        self.assertEqual(len(self.wire_calls), 1)

    async def test_main_receive_loop_verifies_registration_and_clears_ready_on_disconnect(self):
        self.receiver.cancel()
        await asyncio.gather(self.receiver, return_exceptions=True)
        await self.runtime.record_connection(False)
        self.live.current_token, self.live.device_id = 'synthetic-token', 'synthetic-device'
        self.live.background_tasks = set()
        self.live.connection_manager.handle_heartbeat_response = lambda packet: False
        self.live._handle_message_with_semaphore = AsyncMock()
        await self.live.init(self.live.ws)
        self.receiver = asyncio.create_task(self.live.consume_socket(self.live.ws))
        for _ in range(100):
            async with self.sessions() as session:
                state = account_policy.snapshot(await session.get(XYAccount, 1))
            if state['business_state'] == 'ready':
                break
            await asyncio.sleep(.01)
        self.assertEqual(state['business_state'], 'ready')
        self.assertEqual(state['connection_state'], 'connected')
        self.assertIsNone(self.live._registration_mid)
        await self.live.ws.close()
        await asyncio.wait_for(self.receiver, 1)
        async with self.sessions() as session:
            state = account_policy.snapshot(await session.get(XYAccount, 1))
        self.assertEqual(state['connection_state'], 'disconnected')
        self.assertEqual(state['business_state'], 'unchecked')

    async def test_manual_takeover_during_budget_wait_cancels_automatic_send(self):
        from app.services.xianyu.auto_reply_service import AutoReplyService
        from common.services.reply_state import ReplyState
        reply=AutoReplyService('fixture',self.live)
        reply.reply_state=ReplyState(self.sessions)
        await self.budget.defer('fixture',.25)
        sending=asyncio.create_task(reply._send_text_with_separator(self.live.ws,'chat','buyer','queued reply'))
        await asyncio.sleep(.05)
        self.assertEqual(self.wire_calls,[])
        await reply.reply_state.pause('fixture','chat',5)
        results=await asyncio.wait_for(sending,2)
        self.assertFalse(results[0]['success'], results)
        self.assertTrue(results[0]['definitely_not_sent'])
        self.assertEqual(self.wire_calls,[])
        self.assertEqual(self.live._pending_mid_futures,{})

    async def test_raw_message_budget_rejection_is_unsent_and_cleans_future(self):
        await self.budget.defer('fixture',60)
        result=await self.live.send_raw_message(self.live.ws,{
            'lwp':'/r/MessageSend/sendByReceiverScope','headers':{'mid':'raw-fixture'},'body':[]})
        self.assertFalse(result['success'])
        self.assertTrue(result.get('definitely_not_sent'),result)
        self.assertEqual(self.live._pending_mid_futures,{})
        self.assertEqual(self.wire_calls,[])

    async def test_http_429_cools_down_subsequent_socket_business(self):
        async with self.live.session.post(self.http_url+'?status=429') as response:
            self.assertEqual(response.status,429)
        result=await self.live.send_msg(self.live.ws,'chat','buyer','later')
        self.assertFalse(result['success'],result)
        self.assertEqual(result['error_code'],'budget_wait')
        self.assertGreater(result['retry_after'],43)
        self.assertEqual(self.wire_calls,[])
