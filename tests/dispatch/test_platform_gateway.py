"""S1/S2: a web/scheduler MTOP request reaches the existing account worker."""
import unittest
from unittest.mock import patch
from fastapi import FastAPI
import httpx
import test_mtop as fixtures
from common.services.account_dispatch import AccountDispatchClient


class PlatformGatewayTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MtopTests.asyncSetUp
    asyncTearDown = fixtures.MtopTests.asyncTearDown
    asyncTearDownBase = fixtures.MtopTests.asyncTearDownBase
    request = fixtures.MtopTests.request
    gateway_fixture = fixtures.MtopTests.gateway_fixture

    async def test_request_uses_same_executor_and_never_caller_cookie_or_proxy(self):
        from common.services.platform_rpc import PlatformGateway
        from app.api.routes.account_operations import router, get_account_dispatcher
        from app.api.deps import require_internal_auth
        app=FastAPI();app.include_router(router)
        app.dependency_overrides[get_account_dispatcher]=lambda:self.dispatcher
        app.dependency_overrides[require_internal_auth]=lambda:None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            gateway=PlatformGateway(AccountDispatchClient('http://fixture','fixture-'*8,http=http),self.sessions)
            result=await gateway.call('fixture',7,'mtop.idle.pc.idleitem.preget','1.0',{},request_id='platform-1')
            self.assertTrue(result['success'])
            self.assertEqual(self.received[0][0],'unb=101; _m_h5_tk=offline_fixture;')
            self.assertNotIn('cookies_str',result)
            repeated=await gateway.call('fixture',7,'mtop.idle.pc.idleitem.preget','1.0',{},request_id='platform-1')
            self.assertTrue(repeated['success']);self.assertEqual(len(self.received),1)
            with self.assertRaises(ValueError):
                await gateway.call('fixture',7,'mtop.unlisted.operation','1.0',{})
            self.assertEqual(len(self.received),1)

    async def test_existing_mtop_entry_routes_to_worker_over_http(self):
        from aiohttp import web
        from app.api.routes.account_operations import router, get_account_dispatcher
        from app.api.deps import require_internal_auth
        from app.core.config import get_settings
        from common.services.xianyu_mtop import mtop_call
        app=FastAPI();app.include_router(router)
        app.dependency_overrides[get_account_dispatcher]=lambda:self.dispatcher
        app.dependency_overrides[require_internal_auth]=lambda:None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as asgi:
            async def bridge(request):
                response=await asgi.post('/internal/account-operations',json=await request.json())
                return web.json_response(response.json(),status=response.status_code)
            service=web.Application();service.router.add_post('/internal/account-operations',bridge)
            runner=web.AppRunner(service);await runner.setup()
            site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
            try:
                url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
                with patch('common.db.session.async_session_maker',self.sessions), \
                     patch.object(get_settings(),'websocket_service_url',url,create=True), \
                     patch.object(get_settings(),'internal_api_token','fixture-'*8):
                    result=await mtop_call('fixture','CALLER_STALE_COOKIE','mtop.idle.pc.idleitem.preget','1.0',{},owner_id=7)
                self.assertTrue(result['success'],result)
                self.assertEqual(self.received[0][0],'unb=101; _m_h5_tk=offline_fixture;')
                self.assertEqual(len(self.received),1)
            finally:
                await runner.cleanup()

    async def test_catalog_adapter_uses_executor_and_rejects_malformed_pages(self):
        from common.utils.item_info_manager import ItemInfoManager
        from app.services.account_dispatcher import AccountDispatcher
        dispatcher=AccountDispatcher(self.manager,self.sessions,self.budget)
        async def catalog(context,payload):
            manager=ItemInfoManager('fixture','STALE',owner_id=7)
            return await manager.get_item_list_info(1,20,myid='101')
        dispatcher.register('sync_items',catalog)
        self.result={'ret':['SUCCESS::调用成功'],'data':{'cardList':[
            {'cardData':{'id':'item-1','title':'Fixture','priceInfo':{'price':'12'}}}]}}
        result=await dispatcher.execute(self.request('sync_items','catalog-1'))
        self.assertEqual(result.status,'confirmed',result)
        self.assertEqual(result.result['items'][0]['id'],'item-1')
        self.assertEqual(self.received[0][0],'unb=101; _m_h5_tk=offline_fixture;')
        self.assertEqual(len(self.received),1)
        self.redis.now+=2000
        self.result={'ret':['SUCCESS::调用成功'],'data':{}}
        result=await dispatcher.execute(self.request('sync_items','catalog-2'))
        self.assertFalse(result.result['success'])
        self.assertEqual(result.result['error'],'catalog_schema_error')
        self.assertEqual(len(self.received),2)

    async def test_image_upload_uses_worker_cookie_and_fixed_endpoint(self):
        import base64, io
        from PIL import Image
        from common.services import image_gateway
        from common.services.account_dispatch import DispatchRequest
        buffer=io.BytesIO();Image.new('RGB',(4,4),'white').save(buffer,format='PNG')
        self.result={'object':{'url':'https://img.alicdn.com/imgextra/fixture.png'}}
        request=DispatchRequest(request_id='image-1',owner_id=7,account_id='fixture',
            generation=self.runtime.generation,credential_version=0,config_version=0,
            command='upload_image',payload={'content':base64.b64encode(buffer.getvalue()).decode()})
        from common.services.xianyu_mtop import MTOP_BASE
        with patch.object(image_gateway,'UPLOAD_URL',MTOP_BASE+'/upload'):
            result=await self.dispatcher.execute(request)
        self.assertEqual(result.status,'confirmed',result)
        self.assertEqual(result.result['url'],'https://img.alicdn.com/imgextra/fixture.png')
        self.assertEqual(self.received[0][0],'unb=101; _m_h5_tk=offline_fixture;')
        self.assertEqual(len(self.received),1)

    async def test_catalog_partial_failure_keeps_items_but_not_success(self):
        from common.utils.item_info_manager import ItemInfoManager
        from app.services.account_dispatcher import AccountDispatcher
        dispatcher = AccountDispatcher(self.manager, self.sessions, self.budget)
        async def catalog(context, payload):
            manager = ItemInfoManager('fixture', 'STALE', owner_id=7)
            return await manager.get_all_items(page_size=1)
        dispatcher.register('sync_items', catalog)
        self.page_responses = [
            {'ret':['SUCCESS::OK'], 'data':{'cardList':[
                {'cardData':{'id':'item-1','title':'Fixture','priceInfo':{'price':'12'}}}]}},
            {'ret':['FAIL_SYS_SESSION_EXPIRED'], 'data':{}}]
        result = await dispatcher.execute(self.request('sync_items', 'partial-catalog'))
        self.assertFalse(result.result['success'])
        self.assertTrue(result.result['partial'])
        self.assertEqual(result.result['failed_page'], 2)
        self.assertEqual(result.result['total_count'], 1)
        self.assertEqual(result.result['error'], 'invalid_credentials')
        self.assertEqual(result.result['items'][0]['id'], 'item-1')
        self.assertEqual(len(self.received), 2)

    async def test_catalog_repeated_full_page_stops_without_duplicate_items(self):
        from common.utils.item_info_manager import ItemInfoManager
        from app.services.account_dispatcher import AccountDispatcher
        dispatcher = AccountDispatcher(self.manager, self.sessions, self.budget)
        async def catalog(context, payload):
            return await ItemInfoManager('fixture', 'STALE', owner_id=7).get_all_items(page_size=1, max_pages=3)
        dispatcher.register('sync_items', catalog)
        page = {'ret':['SUCCESS::OK'], 'data':{'cardList':[{'cardData':{'id':'same','priceInfo':{}}}]}}
        self.page_responses = [page, page, page]
        result = await dispatcher.execute(self.request('sync_items', 'stalled-catalog'))
        self.assertFalse(result.result['success'])
        self.assertEqual(result.result['error'], 'catalog_pagination_stalled')
        self.assertEqual(result.result['total_count'], 1)
        self.assertEqual(len(self.received), 2)

    async def test_normal_photo_above_256kb_passes_upload_contract(self):
        import base64, io, random
        from PIL import Image
        from common.services import image_gateway
        from common.services.account_dispatch import DispatchRequest
        from common.services.xianyu_mtop import MTOP_BASE
        image = Image.frombytes('RGB', (400,400), random.Random(1).randbytes(400*400*3))
        buffer = io.BytesIO(); image.save(buffer, format='PNG')
        self.assertGreater(len(buffer.getvalue()), 256*1024)
        self.result = {'object': {'url': 'https://img.alicdn.com/imgextra/photo.png'}}
        request = DispatchRequest(request_id='large-photo', owner_id=7, account_id='fixture',
            generation=self.runtime.generation, credential_version=0, config_version=0,
            command='upload_image', payload={'content':base64.b64encode(buffer.getvalue()).decode()})
        with patch.object(image_gateway, 'UPLOAD_URL', MTOP_BASE + '/upload'):
            result = await self.dispatcher.execute(request)
        self.assertEqual(result.status, 'confirmed', result)
        self.assertEqual((result.result['width'], result.result['height']), (400,400))
        self.assertEqual(len(self.received), 1)

    async def test_multiple_catalog_requests_wait_without_sending_duplicate_pages(self):
        import time
        from common.services import account_policy
        from common.models.xy_account import XYAccount
        from common.services.xianyu_mtop import mtop_call
        from app.services.account_dispatcher import AccountDispatcher
        async with self.sessions() as session:
            account = await session.get(XYAccount, 1)
            state = account_policy.snapshot(account)
            state['config_values']['risk'] = {'min_interval_seconds':.12}
            account_policy.store(account, state)
            await session.commit()
        self.advance_page_clock = False
        self.page_responses = [{'ret':['SUCCESS::OK'], 'data':{'page':n}} for n in (1, 2, 3)]
        dispatcher = AccountDispatcher(self.manager, self.sessions, self.budget)
        async def catalog(context, payload):
            results = []
            for page in (1, 2, 3):
                response = await mtop_call('fixture', 'STALE', 'mtop.idle.web.xyh.item.list', '1.0', {'pageNumber':page}, owner_id=7)
                if not response['success']:
                    return response
                results.append(response['res']['data']['page'])
            return {'success':True, 'pages':results}
        dispatcher.register('sync_items', catalog)
        started = time.monotonic()
        result = await dispatcher.execute(self.request('sync_items', 'paced-catalog'))
        self.assertTrue(result.result['success'], result)
        self.assertEqual(result.result['pages'], [1, 2, 3])
        self.assertGreaterEqual(time.monotonic() - started, .22)
        self.assertEqual(len(self.received), 3)
        repeated = await dispatcher.execute(self.request('sync_items', 'paced-catalog'))
        self.assertEqual(repeated.status, 'confirmed')
        self.assertEqual(len(self.received), 3)

    async def test_gateway_preserves_rate_limit_and_explicit_business_error(self):
        from common.services.xianyu_mtop import mtop_call
        async with self.gateway_fixture():
            self.http_status = 429
            self.response_headers = {'Retry-After':'45'}
            result = await mtop_call('fixture', 'STALE', 'mtop.idle.pc.idleitem.preget', '1.0', {}, owner_id=7)
            self.assertFalse(result['success'])
            self.assertEqual(result['retry_after'], 45)
            self.assertFalse(result['_request_status_unknown'])
            self.http_status, self.response_headers = 200, {}
            self.redis.now += 45000
            self.result = {'ret':['FAIL_BIZ_NO_SHOP::no seller workspace'], 'data':{'token':'SECRET'}}
            result = await mtop_call('fixture', 'STALE', 'mtop.idle.pc.backend.idleitem.preget', '1.0', {}, owner_id=7)
            self.assertEqual(result['res']['ret'], ['FAIL_BIZ_NO_SHOP::no seller workspace'])
            self.assertNotIn('SECRET', str(result['res']))
            self.assertEqual(len(self.received), 2)

    async def test_seller_headers_cross_rpc_without_accepting_caller_auth_headers(self):
        from common.services.xianyu_mtop import mtop_call
        from common.services.platform_rpc import PlatformRequest
        async with self.gateway_fixture():
            result = await mtop_call('fixture', 'STALE', 'mtop.idle.pc.backend.idleitem.preget', '1.0', {}, owner_id=7,
                extra_headers={'idle_site_biz_code':'COMMONPRO', 'idle_user_group_member_id':''},
                origin='https://seller.goofish.com', referer='https://seller.goofish.com/?site=COMMONPRO')
        self.assertTrue(result['success'], result)
        self.assertEqual(self.received_headers[0].get('idle_site_biz_code'), 'COMMONPRO')
        self.assertEqual(self.received_headers[0].get('Origin'), 'https://seller.goofish.com')
        for name in ('Cookie', 'Authorization', 'Host'):
            with self.assertRaises(ValueError):
                PlatformRequest(api='mtop.idle.pc.backend.idleitem.preget', version='1.0', data={}, extra_headers={name:'INJECTED'})
        self.assertEqual(len(self.received), 1)

    async def test_capability_limit_stops_catalog_without_guessing_account_type(self):
        from common.services.item_service import ItemService
        from common.models.xy_account import XYAccount
        self.http_status = 429
        self.response_headers = {'Retry-After':'45'}
        async with self.gateway_fixture():
            async with self.sessions() as session:
                account = await session.get(XYAccount, 1)
                result = await ItemService(session).fetch_items_page_from_account(account)
        self.assertFalse(result['success'])
        self.assertEqual(result['status'], 'rate_limited', result)
        self.assertEqual(result['retry_after'], 45)
        self.assertEqual(result['saved_count'], 0)
        self.assertEqual(len(self.received), 1)

    async def test_pending_rate_list_uses_worker_and_never_relogs_or_replays_failure(self):
        from common.services.rate_service import fetch_merchant_rate_list
        self.result = {'ret':['SUCCESS::OK'], 'data':{'module':{'items':[{'tradeId':'order-1'}], 'totalCount':'1'}}}
        async with self.gateway_fixture():
            # The already-created worker session is the only permitted HTTP connection.
            with patch('common.services.rate_service.aiohttp.ClientSession', side_effect=AssertionError('existing_executor_required')):
                result = await fetch_merchant_rate_list('STALE', 'fixture', max_retries=1)
                self.assertTrue(result['success'], result)
                self.assertEqual(result['total_count'], 1)
                self.assertEqual(result['items'][0]['tradeId'], 'order-1')
                self.redis.now += 2000
                self.result = {'ret':['FAIL_SYS_SESSION_EXPIRED::expired']}
                with patch('common.utils.cookie_refresh.trigger_password_login_async') as login:
                    result = await fetch_merchant_rate_list('STALE', 'fixture', max_retries=3)
                login.assert_not_called()
                self.assertFalse(result['success'])
                self.assertEqual(result['message'], 'invalid_credentials')
                self.assertEqual(len(self.received), 2)

    async def test_product_image_upload_uses_the_account_executor_not_callers_cookie(self):
        from io import BytesIO
        from PIL import Image
        from common.services import image_gateway
        from common.services.xianyu_publish_media import upload_publish_image_content
        image=BytesIO();Image.new('RGB',(2,3)).save(image,format='PNG')
        self.result={'success':True,'object':{'url':'https://img.alicdn.com/fixture.png','pix':'2x3'}}
        async with self.gateway_fixture():
            with patch.object(image_gateway,'PRODUCT_UPLOAD_URL',self.base.new+'/product-image',create=True):
                result=await upload_publish_image_content(image.getvalue(),'fixture.png','STALE_COOKIE',account_id='fixture',owner_id=7)
        self.assertEqual((result['widthSize'],result['heightSize']),(2,3))
        self.assertEqual(result['url'],'https://img.alicdn.com/fixture.png')
        self.assertEqual(len(self.received),1)
        self.assertEqual(self.received[0][0],'unb=101; _m_h5_tk=offline_fixture;')

    async def test_explicit_no_shop_result_allows_personal_capability_over_same_executor(self):
        from common.services.xianyu_publish_service import detect_publish_account_capability
        self.page_responses = [
            {'ret':['FAIL_BIZ_NO_SHOP::no seller workspace']},
            {'ret':['SUCCESS::OK'], 'data':{'commissionConfig':{
                'defaultCommissionTitle':'基础软件服务费'}, 'supportSkuOrInventory':False}},
        ]
        async with self.gateway_fixture():
            result = await detect_publish_account_capability('STALE', account_id='fixture', owner_id=7)
        self.assertTrue(result['success'], result)
        self.assertFalse(result['is_fish_shop'])
        self.assertTrue(result['detection_reliable'])
        self.assertEqual(len(self.received), 2)
        self.assertTrue(all(row[0]=='unb=101; _m_h5_tk=offline_fixture;' for row in self.received))

    async def test_unknown_business_error_does_not_imply_personal_seller(self):
        from common.services.xianyu_publish_service import detect_publish_account_capability
        self.page_responses = [
            {'ret':['FAIL_BIZ_TEMPORARY_FAILURE::try later']},
            {'ret':['SUCCESS::OK'], 'data':{'commissionConfig':{
                'defaultCommissionTitle':'基础软件服务费'}, 'supportSkuOrInventory':False}},
        ]
        async with self.gateway_fixture():
            result = await detect_publish_account_capability('STALE', account_id='fixture', owner_id=7)
        self.assertFalse(result['success'], result)
        self.assertFalse(result['detection_reliable'])
        self.assertEqual(len(self.received), 1)
