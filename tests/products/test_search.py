import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from stdlib_loader import load

class SearchTests(unittest.IsolatedAsyncioTestCase):
    def service(self, **kw):
        m=load('backend-web/app/services/search/searcher.py', asyncio=asyncio,
               BrowserManager=Mock, ItemParser=Mock, SliderHandler=Mock,
               PLAYWRIGHT_AVAILABLE=True,
               product_error_status=load('common/services/product_results.py').product_error_status)
        return m.ItemSearchService(**kw)

    async def test_missing_owner_does_not_query_any_account(self):
        db=SimpleNamespace(execute=AsyncMock())
        svc=self.service(db_session=db)
        self.assertIsNone(await svc.get_first_valid_cookie())
        db.execute.assert_not_awaited()

    async def test_multi_page_partial_error_preserves_results(self):
        svc=self.service(user_id='1')
        svc.search_items=AsyncMock(side_effect=[
            {'items':[{'item_id':'x'},{'item_id':'y'}],'total':2},
            {'items':[{'item_id':'y'},{'item_id':'z'}],'total':2},
            {'items':[], 'error':'限流', 'status':'rate_limited', 'retry_after':60}])
        r=await svc.search_multiple_pages('book',3)
        self.assertEqual([i['item_id'] for i in r['items']],['x','y','z'])
        self.assertEqual(r['status'],'rate_limited'); self.assertEqual(r['failed_page'],3)
        self.assertEqual(r['retry_after'],60)

    async def test_capture_missing_result_list_is_error(self):
        svc=self.service(user_id='1')
        response=SimpleNamespace(url='h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search',status=200,
                                 json=AsyncMock(return_value={'ret':['SUCCESS::调用成功'],'data':{}}))
        await svc._on_response(response)
        self.assertEqual(getattr(svc,'response_error',{}).get('status'),'schema_error')

    async def test_capture_expired_credentials_not_empty(self):
        svc=self.service(user_id='1')
        response=SimpleNamespace(url='h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search',status=200,
                                 json=AsyncMock(return_value={'ret':['FAIL_SYS_SESSION_EXPIRED']}))
        await svc._on_response(response)
        self.assertEqual(getattr(svc,'response_error',{}).get('status'),'credentials_expired')
