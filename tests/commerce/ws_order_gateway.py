"""旧OrderService -> 账号执行方HTTP -> 本地平台夹具。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import aiohttp
from aiohttp import web
import httpx
from fastapi import FastAPI
import test_fulfillment as fixtures
from app.api.deps import require_internal_auth
from app.services.shipping import commerce_routes,order_platform_routes
from common.services import order_platform_gateway as gateway
from common.services.order_service import OrderService
from common.services.account_dispatch import AccountDispatchClient

class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.FulfillmentTests.asyncSetUp(self)
        self.requests=[]
        async def platform(request):
            self.requests.append((request.headers.get('cookie'),await request.post()))
            return web.json_response({'ret':['SUCCESS'],'data':{'module':{'items':[],'totalCount':'0','nextPage':'false'}}})
        app=web.Application();app.router.add_post('/{tail:.*}',platform)
        self.runner=web.AppRunner(app);await self.runner.setup();site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        self.url='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
        self.live=SimpleNamespace(cookies_str='CURRENT_FIXTURE_COOKIE',_check_account_execution=AsyncMock(),_build_session_connector=lambda:aiohttp.TCPConnector(),_account_runtime=SimpleNamespace(owner_id=1,account_id='a'))
        app=FastAPI();app.include_router(commerce_routes.router);app.dependency_overrides[require_internal_auth]=lambda:None
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://executor')
    async def asyncTearDown(self):
        await self.http.aclose();await self.runner.cleanup();await self.engine.dispose()
    async def test_old_history_fetch_runs_at_executor_with_current_credentials(self):
        client=AccountDispatchClient('http://executor','f'*32,http=self.http)
        with patch.object(gateway,'async_session_maker',self.sessions),patch.object(order_platform_routes,'async_session_maker',self.sessions),patch.object(order_platform_routes,'take_budget',AsyncMock()),patch.object(order_platform_routes,'PLATFORM_BASE',self.url),patch('app.core.config.get_settings',return_value=SimpleNamespace(websocket_service_url='http://executor',internal_api_token='f'*32)),patch.object(gateway,'AccountDispatchClient',return_value=client),patch('app.services.xianyu.cookie_manager.get_manager',return_value=SimpleNamespace(instances={'a':self.live})):
            async with self.sessions() as s:
                result=await OrderService(s)._fetch_sold_orders_page('STALE_SECRET',1,account_id='a')
        self.assertEqual(result['items'],[]);self.assertEqual(len(self.requests),1)
        self.assertEqual(self.requests[0][0],'CURRENT_FIXTURE_COOKIE')
        self.assertEqual(__import__('json').loads(self.requests[0][1]['data'])['rowsPerPage'],30)
        self.assertNotIn('STALE_SECRET',str(self.requests));self.assertNotIn('cookies_str',result)
    async def test_stale_generation_stops_before_external_request(self):
        with patch.object(order_platform_routes,'async_session_maker',self.sessions),patch.object(order_platform_routes,'take_budget',AsyncMock()),patch.object(order_platform_routes,'PLATFORM_BASE',self.url),patch('app.services.xianyu.cookie_manager.get_manager',return_value=SimpleNamespace(instances={'a':self.live})):
            result=await self.http.post('/internal/orders/platform-read',json={'owner_id':1,'account_id':'a','generation':99,'credential_version':0,'config_version':0,'operation':'sold'})
        self.assertEqual(result.status_code,409);self.assertEqual(self.requests,[])

if __name__=='__main__':unittest.main()
