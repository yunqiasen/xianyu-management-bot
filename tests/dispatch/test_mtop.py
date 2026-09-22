"""S1 real dispatcher -> existing HTTP session -> local S2 platform fixture."""
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch
import aiohttp
from aiohttp import web
import test_flow as fixtures
from common.services.account_dispatch import DispatchError
from common.services import xianyu_mtop

class MtopTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDownBase = fixtures.FlowTests.asyncTearDown
    request = fixtures.FlowTests.request

    async def asyncSetUp(self):
        await fixtures.FlowTests.asyncSetUp(self)
        self.received=[]
        self.received_headers=[]
        self.response_headers={}
        self.result={'ret':['SUCCESS::OK'],'data':{'items':[]}}
        self.http_status=200
        async def platform(request):
            self.received.append((request.headers['Cookie'],await request.post()))
            self.received_headers.append(dict(request.headers))
            responses = getattr(self, 'page_responses', None)
            if responses:
                if getattr(self, 'advance_page_clock', True):
                    self.redis.now += 2000
                return web.json_response(responses.pop(0), status=self.http_status)
            return web.json_response(self.result,status=self.http_status, headers=self.response_headers)
        app=web.Application();app.router.add_post('/{path:.*}',platform)
        self.server=web.AppRunner(app);await self.server.setup()
        site=web.TCPSite(self.server,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        self.base=patch.object(xianyu_mtop,'MTOP_BASE',f'http://127.0.0.1:{port}');self.base.start()
        self.live.session=aiohttp.ClientSession(trust_env=False)
        self.live._get_proxy_url=lambda:None
        async def operation(context,payload):
            result=await xianyu_mtop.mtop_call('fixture','STALE_COOKIE','fixture.read','1.0',{},owner_id=7)
            if not result['success']:
                raise DispatchError(result['error'])
            return result['res']
        self.dispatcher.register('sync_items',operation)

    async def asyncTearDown(self):
        self.base.stop();await self.live.session.close();await self.server.cleanup()
        await self.asyncTearDownBase()

    @asynccontextmanager
    async def gateway_fixture(self):
        import httpx
        from fastapi import FastAPI
        from app.api.routes.account_operations import router, get_account_dispatcher
        from app.api.deps import require_internal_auth
        from app.core.config import get_settings
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[get_account_dispatcher] = lambda:self.dispatcher
        app.dependency_overrides[require_internal_auth] = lambda:None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as http:
            async def bridge(request):
                response = await http.post('/internal/account-operations', json=await request.json())
                return web.json_response(response.json(), status=response.status_code)
            service = web.Application(); service.router.add_post('/internal/account-operations', bridge)
            server = web.AppRunner(service); await server.setup()
            site = web.TCPSite(server, '127.0.0.1', 0); await site.start()
            try:
                with patch('common.db.session.async_session_maker', self.sessions), \
                     patch.object(get_settings(), 'websocket_service_url', f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}', create=True), \
                     patch.object(get_settings(), 'internal_api_token', 'fixture-' * 8):
                    yield
            finally:
                await server.cleanup()

    async def test_success_uses_executor_cookie_and_replay_does_not_resend(self):
        result=await self.dispatcher.execute(self.request('sync_items'))
        self.assertEqual(result.status,'confirmed')
        await self.dispatcher.execute(self.request('sync_items'))
        self.assertEqual(len(self.received),1)
        self.assertEqual(self.received[0][0],'unb=101; _m_h5_tk=offline_fixture;')

    async def test_ambiguous_http_response_is_not_retried(self):
        self.http_status=503
        result=await self.dispatcher.execute(self.request('sync_items'))
        self.assertEqual(result.status,'unknown')
        self.assertEqual(len(self.received),1)

    async def test_expired_cookie_does_not_launch_password_login_or_replay(self):
        self.result={'ret':['FAIL_SYS_SESSION_EXPIRED']}
        with patch('common.utils.cookie_refresh.trigger_password_login_async') as login:
            result=await self.dispatcher.execute(self.request('sync_items'))
        login.assert_not_called();self.assertEqual(len(self.received),1)
        self.assertEqual(result.error_code,'invalid_credentials')
