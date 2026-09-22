import json
import unittest
from unittest.mock import patch, AsyncMock
import httpx
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from common.models.xy_account import XYAccount


class SearchGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_routes_through_account_worker_instead_of_new_browser(self):
        from app.services.search.searcher import ItemSearchService
        from app.core.config import get_settings
        engine=create_async_engine('sqlite+aiosqlite:///:memory:')
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        async with engine.begin() as connection:await connection.run_sync(XYAccount.__table__.create)
        async with sessions() as session:
            session.add(XYAccount(id=1,owner_id=7,account_id='search-fixture',unb='101',cookie='unb=101; fixture='+('x'*70),
                                  login_method='cookie',status='active'));await session.commit()
        requests=[]
        async def worker(request):
            body=json.loads(request.content);requests.append(body)
            return httpx.Response(200,json={**{key:body[key] for key in ('request_id','owner_id','account_id','command',
                'generation','credential_version','config_version')},'status':'confirmed','result':{
                    'ret':['SUCCESS::调用成功'],'data':{'resultList':[],'resultInfo':{'hasNextPage':False}}}})
        real_client=httpx.AsyncClient
        def client(*args,**kwargs):return real_client(*args,transport=httpx.MockTransport(worker),**kwargs)
        try:
            async with sessions() as session:
                search=ItemSearchService(session,user_id='7',account_id='search-fixture')
                with patch.object(search.browser,'init_browser',AsyncMock(side_effect=AssertionError('unexpected_browser'))), \
                     patch('common.db.session.async_session_maker',sessions),patch('httpx.AsyncClient',client), \
                     patch.object(get_settings(),'internal_api_token','fixture-'*8):
                    result=await search.search_items('fixture',page=2,page_size=20)
                self.assertEqual(result.get('source'),'account_executor',result)
                self.assertEqual(result['items'],[])
                self.assertEqual(len(requests),1)
                self.assertEqual(requests[0]['payload']['data']['pageNumber'],2)
                self.assertNotIn('cookie',requests[0]['payload'])
        finally:await engine.dispose()
