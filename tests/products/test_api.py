"""S1 HTTP -> real services -> disposable SQLite. Platform calls are S2 fixtures."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.api import deps
from app.api.routes import items, product_publish
from common.models.product_material import ProductMaterial
from common.models.publish_log import PublishLog
from common.models.xy_account import XYAccount
from common.models.product_polish_schedule import ProductPolishSchedule
from common.services.product_polish_schedule import ProductPolishScheduleService
from common.services import publish_execution_service as execution

@compiles(BigInteger, 'sqlite')
def sqlite_bigint(element, compiler, **kw): return 'INTEGER'

class ProductApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine=create_async_engine('sqlite+aiosqlite:///:memory:')
        self.sessions=async_sessionmaker(self.engine,expire_on_commit=False)
        async with self.engine.begin() as conn:
            for table in [ProductMaterial.__table__,PublishLog.__table__,XYAccount.__table__,ProductPolishSchedule.__table__]:
                await conn.run_sync(table.create)
        from common.models.product_reliability_migration import upgrade_products
        async with self.engine.begin() as conn:
            await conn.run_sync(upgrade_products)
        async with self.sessions() as s:
            s.add_all([XYAccount(id=i,owner_id=i,account_id=f'a{i}',unb=str(i),cookie=f'unb={i}; fixture='+'x'*60,
                        login_method='cookie',status='active',auto_polish=False) for i in (1,2)])
            await s.commit()
        self.user=SimpleNamespace(id=1,role='USER')
        async def database():
            async with self.sessions() as s: yield s
        self.app=FastAPI()
        self.app.include_router(items.items_router,prefix='/api/v1')
        self.app.include_router(product_publish.router,prefix='/api/v1')
        self.app.dependency_overrides[deps.get_db_session]=database
        self.app.dependency_overrides[deps.get_current_active_user]=lambda: self.user
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://fixture')

    async def asyncTearDown(self):
        await self.client.aclose(); await self.engine.dispose()

    def material(self):
        return dict(title='fixture',description='fixture description',price=12,images=['/static/fixture.png'],
                    specifications=[dict(name='颜色',source_id='p1',values=[dict(name='蓝',source_id='v1')])],
                    sku_rows=[dict(source_id='s1',specs={'颜色':'蓝'},price=12,stock=2)])

    async def test_material_create_read_update_reject_and_owner_isolation(self):
        response=await self.client.post('/api/v1/product-publish/materials',json=self.material())
        self.assertEqual(response.status_code,200,response.text)
        body=response.json(); self.assertTrue(body['success'],body); mid=body['data']['id']
        url=f'/api/v1/product-publish/materials/{mid}'
        data=(await self.client.get(url)).json()['data']
        self.assertEqual(data['sku_rows'][0]['source_id'],'s1')
        bad=await self.client.put(url,json={'specifications':[{'name':'颜色','values':[{'name':'红'}]}]})
        self.assertFalse(bad.json()['success'])
        self.assertEqual((await self.client.get(url)).json()['data']['specifications'][0]['values'][0]['name'],'蓝')
        self.user.id=2
        hidden=(await self.client.get(url)).json()
        self.assertFalse(hidden['success'])

    async def test_search_partial_pages_through_http(self):
        with patch('app.services.search.searcher.ItemSearchService.search_items',new=AsyncMock(side_effect=[
            {'items':[{'item_id':'1'},{'item_id':'2'}]},
            {'items':[{'item_id':'2'},{'item_id':'3'}]},
            {'items':[],'error':'限流','status':'rate_limited'}])):
            r=await self.client.post('/api/v1/items/search',json={'keyword':'fixture','total_pages':3,'account_id':'a1'})
        data=r.json(); self.assertFalse(data['success']); self.assertEqual(data['status'],'rate_limited')
        self.assertEqual([i['item_id'] for i in data['data']],['1','2','3'])

    async def test_search_cookie_lookup_never_crosses_owner(self):
        from app.services.search.searcher import ItemSearchService
        async with self.sessions() as s:
            service=ItemSearchService(s,user_id='1',account_id='a2')
            self.assertIsNone(await service.get_first_valid_cookie())
            service.account_id='a1'
            self.assertEqual((await service.get_first_valid_cookie())['id'],'a1')

    async def test_window_form_preserves_switch_and_restart_claim(self):
        url='/api/v1/items/polish-window/a1'
        response=await self.client.put(url,json={'timezone_name':'Asia/Shanghai','start':'23:00','end':'01:00'})
        self.assertTrue(response.json()['success'],response.text)
        self.assertFalse((await self.client.get(url)).json()['data']['enabled'])
        async with self.sessions() as s:
            account=(await s.execute(select(XYAccount).where(XYAccount.id==1))).scalar_one()
            account.auto_polish=True; await s.commit()
            first=await ProductPolishScheduleService(s).admit(account,datetime(2026,9,21,15,30,tzinfo=timezone.utc))
            self.assertEqual(first['status'],'ready')
        async with self.sessions() as s:
            account=(await s.execute(select(XYAccount).where(XYAccount.id==1))).scalar_one()
            second=await ProductPolishScheduleService(s).admit(account,datetime(2026,9,21,16,30,tzinfo=timezone.utc))
            self.assertEqual(second['status'],'already_processed')
        self.user.id=2
        self.assertEqual((await self.client.get(url)).status_code,404)

    async def test_single_timeout_persisted_and_http_retry_not_resent(self):
        data={**self.material(),'account_id':'a1','publish_request_id':'request-fixture-01','address':'fixture'}
        address=SimpleNamespace(apply_to_item_data=lambda data:dict(data),to_log_fields=lambda:{})
        publish=AsyncMock(side_effect=TimeoutError())
        with patch.object(execution,'async_session_maker',self.sessions), \
             patch.object(execution.PublishAddressService,'resolve_publish_address',new=AsyncMock(return_value=address)), \
             patch.object(execution,'detect_publish_account_capability',new=AsyncMock(return_value={'success':True,'is_fish_shop':True})), \
             patch.object(execution,'ensure_publish_capability_reliable',new=lambda r:r), \
             patch.object(execution,'publish_single_item',new=publish):
            first=(await self.client.post('/api/v1/product-publish/publish/single',json=data)).json()
            second=(await self.client.post('/api/v1/product-publish/publish/single',json=data)).json()
        self.assertTrue(first['data']['unknown'],first); self.assertTrue(second['data']['unknown'],second)
        self.assertEqual(publish.await_count,1)
        async with self.sessions() as s:
            log=(await s.execute(select(PublishLog))).scalar_one()
            self.assertEqual(log.status,'unknown')
            self.assertEqual(log.publish_snapshot['sku_rows'][0]['source_id'],'s1')

    async def test_disabled_account_publish_stops_before_platform(self):
        async with self.sessions() as s:
            account=(await s.execute(select(XYAccount).where(XYAccount.id==1))).scalar_one()
            account.status='inactive'; await s.commit()
        data={**self.material(),'account_id':'a1','publish_request_id':'disabled-request-01'}
        with patch.object(execution,'detect_publish_account_capability',new=AsyncMock()) as external, \
             patch.object(execution.PublishAddressService,'resolve_publish_address',new=AsyncMock(side_effect=ValueError('fixture address'))) as address:
            r=(await self.client.post('/api/v1/product-publish/publish/single',json=data)).json()
        self.assertFalse(r['success']); external.assert_not_awaited(); address.assert_not_awaited()

    async def test_real_sync_db_keeps_first_page_on_second_page_error(self):
        from common.services.item_service import ItemService
        from common.models.xy_catalog_item import XYCatalogItem
        async with self.engine.begin() as conn:
            await conn.run_sync(XYCatalogItem.__table__.create)
        manager=SimpleNamespace(close=AsyncMock(),get_item_list_info=AsyncMock(side_effect=[
            {'success':True,'items':[{'id':'1','title':'one','price':'12'},{'id':'2','title':'two','price':'13'}]},
            {'success':False,'message':'FAIL_SYS_TRAFFIC_LIMIT'}]))
        async with self.sessions() as s:
            account=(await s.execute(select(XYAccount).where(XYAccount.id==1))).scalar_one()
            svc=ItemService(s)
            with patch.object(svc,'_resolve_item_fetch_manager',new=AsyncMock(return_value=manager)), \
                 patch('common.services.item_service.asyncio.sleep',new=AsyncMock()):
                r=await svc._fetch_all_items_from_account_impl(account,page_size=2)
            self.assertFalse(r['success']); self.assertTrue(r.get('partial'),r)
            self.assertEqual(r['saved_count'],2)
        async with self.sessions() as s:
            rows=(await s.execute(select(XYCatalogItem))).scalars().all()
            self.assertEqual({row.item_id for row in rows},{'1','2'})
            self.assertEqual({row.owner_id for row in rows},{1})

    async def test_failed_publish_log_claim_only_one_retry_wins(self):
        from common.services.publish_log_service import PublishLogService
        async with self.sessions() as s:
            log=await PublishLogService(s).create_log(user_id=1,account_id='a1',title='fixture',status='failed',publish_snapshot={'title':'original'})
            log_id=log.id
        async with self.sessions() as first, self.sessions() as second:
            self.assertTrue(await PublishLogService(first).claim_failed(log_id))
            self.assertFalse(await PublishLogService(second).claim_failed(log_id))
        async with self.sessions() as s:
            row=await s.get(PublishLog,log_id)
            self.assertEqual(row.publish_snapshot,{'title':'original'})

    async def test_publish_snapshot_survives_material_edit(self):
        from common.services.publish_log_service import PublishLogService
        from app.services.product_publish_service import ProductMaterialService, _material_to_dict
        async with self.sessions() as s:
            material=await ProductMaterialService(s).create(1,self.material())
            log=await PublishLogService(s).create_log(user_id=1,account_id='a1',title='fixture',material_id=material.id,
                                                     publish_snapshot=_material_to_dict(material))
            await ProductMaterialService(s).update(material.id,1,{'title':'edited'})
            self.assertEqual(log.publish_snapshot['title'],'fixture')
            self.assertEqual(log.publish_snapshot['sku_rows'][0]['source_id'],'s1')

    async def test_publish_bad_sku_fails_at_validation_before_execution(self):
        data={**self.material(),'account_id':'a1','publish_request_id':'bad-sku-fixture'}
        data['sku_rows'][0]['specs']['颜色']='不存在'
        with patch('app.services.publish_execution_service.PublishExecutorService.publish_single',new=AsyncMock(return_value={'success':True})) as execute:
            r=await self.client.post('/api/v1/product-publish/publish/single',json=data)
        self.assertEqual(r.status_code,422,r.text); execute.assert_not_awaited()

    async def test_random_window_plans_once_and_survives_restart_before_due_time(self):
        url='/api/v1/items/polish-window/a1'
        response=await self.client.put(url,json={'timezone_name':'UTC','start':'09:00','end':'10:00','randomize':True})
        self.assertEqual(response.status_code,200,response.text)
        config=(await self.client.get(url)).json()['data']
        self.assertTrue(config.get('randomize'),'随机窗设置应往返保留')
        async with self.sessions() as s:
            account=await s.get(XYAccount,1);account.auto_polish=True;await s.commit()
            with patch('secrets.randbelow',return_value=30):
                result=await ProductPolishScheduleService(s).admit(account,datetime(2026,9,23,9,0,tzinfo=timezone.utc))
            self.assertEqual(result['status'],'waiting')
            due=result['planned_at']
            self.assertEqual(due,int(datetime(2026,9,23,9,30,tzinfo=timezone.utc).timestamp()))
        async with self.sessions() as s:
            account=await s.get(XYAccount,1)
            with patch('secrets.randbelow',side_effect=AssertionError('同一周期重复随机')):
                result=await ProductPolishScheduleService(s).admit(account,datetime(2026,9,23,9,29,tzinfo=timezone.utc))
                self.assertEqual(result['status'],'waiting');self.assertEqual(result['planned_at'],due)
                result=await ProductPolishScheduleService(s).admit(account,datetime(2026,9,23,9,30,tzinfo=timezone.utc))
                self.assertEqual(result['status'],'ready')
                result=await ProductPolishScheduleService(s).admit(account,datetime(2026,9,23,9,40,tzinfo=timezone.utc))
                self.assertEqual(result['status'],'already_processed')
