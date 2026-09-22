"""S1 durable product flows; all platform boundaries replaced with fixtures."""
import test_api as fixtures
import unittest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from datetime import datetime
from common.models.publish_log import PublishLog

class DurableBatchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ProductApiTests.asyncSetUp
    asyncTearDown = fixtures.ProductApiTests.asyncTearDown
    material = fixtures.ProductApiTests.material
    async def test_batch_survives_cache_loss_cancel_and_failed_only_retry(self):
        mid=(await self.client.post('/api/v1/product-publish/materials',json=self.material())).json()['data']['id']
        with patch('app.api.routes.product_publish._run_batch_publish_background',new=AsyncMock()):
            r=(await self.client.post('/api/v1/product-publish/publish/batch',json={'account_ids':['a1'],'material_ids':[mid]})).json()
        bid=r['data']['batch_id']
        from app.services.publish_batch_status_service import PublishBatchStatusService
        await PublishBatchStatusService.clear_batch(bid)
        url=f'/api/v1/product-publish/publish/batch/{bid}'
        status=(await self.client.get(url+'/status')).json()
        self.assertTrue(status['success'],status)
        self.assertEqual(status['data']['pending'],1)
        cancel=await self.client.post(url+'/cancel')
        self.assertEqual(cancel.status_code,200,cancel.text)
        self.assertEqual((await self.client.get(url+'/status')).json()['data']['cancelled'],1)
        self.user.id=2
        self.assertFalse((await self.client.get(url+'/status')).json()['success'])

    async def test_reconcile_requires_evidence_and_records_actor(self):
        from common.services.publish_log_service import PublishLogService
        async with self.sessions() as s:
            log=await PublishLogService(s).create_log(user_id=1,account_id='a1',title='fixture',status='unknown',publish_request_id='reconcile-01')
            lid=log.id
        url=f'/api/v1/product-publish/logs/{lid}/reconcile'
        r=await self.client.post(url,json={'outcome':'not_published','evidence':'平台商品列表与发布时间人工核对，未发布'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertTrue(r.json()['success'],r.text)
        async with self.sessions() as s:
            self.assertEqual((await s.get(PublishLog,lid)).status,'failed')
        evidence=(await self.client.get(f'/api/v1/product-publish/logs/{lid}/evidence')).json()
        self.assertEqual(evidence['data'][0]['actor_id'],1)
        self.assertEqual((await self.client.post(url,json={'outcome':'not_published','evidence':''})).status_code,422)

    async def test_persistent_runner_retries_only_confirmed_failure(self):
        from common.services.product_batch_service import ProductBatchService
        from common.services import publish_execution_service as execution
        from types import SimpleNamespace
        address=SimpleNamespace(apply_to_item_data=lambda d:dict(d),to_log_fields=lambda:{})
        async with self.sessions() as s:
            batch=await ProductBatchService(s).create(1,['a1'],[self.material(),self.material(),self.material()])
            bid=batch.id
        external=AsyncMock(side_effect=[{'success':True,'item_id':'100'}, {'success':False,'message':'类目错误'}, TimeoutError()])
        with patch.object(execution,'async_session_maker',self.sessions), \
             patch.object(execution.PublishAddressService,'resolve_publish_address',new=AsyncMock(return_value=address)), \
             patch.object(execution,'detect_publish_account_capability',new=AsyncMock(return_value={'success':True,'is_fish_shop':True})), \
             patch.object(execution,'ensure_publish_capability_reliable',new=lambda r:r), \
             patch.object(execution,'_sync_account_items_after_publish',new=AsyncMock(return_value={})), \
             patch.object(execution,'publish_single_item',new=external):
            async with self.sessions() as s:
                await ProductBatchService(s).run(1,bid)
            url=f'/api/v1/product-publish/publish/batch/{bid}'
            status=(await self.client.get(url+'/status')).json()['data']
            self.assertEqual((status['success'],status['failed'],status['unknown']),(1,1,1))
            external.side_effect=None; external.return_value={'success':True,'item_id':'101'}
            with patch('common.db.session.async_session_maker',self.sessions):
                response=await self.client.post(url+'/retry-failed')
            self.assertTrue(response.json()['success'],response.text)
            self.assertEqual(external.await_count,4)
            async with self.sessions() as s:
                await ProductBatchService(s).run(1,bid)
            self.assertEqual(external.await_count,4)

    async def test_feedback_immediate_history_unknown_and_cooldown(self):
        from app.api.routes import auto_rate
        self.app.include_router(auto_rate.router,prefix='/api/v1/auto-rate')
        from common.models.xy_order import XYOrder
        from common.models.auto_rate_config import AutoRateConfig
        async with self.engine.begin() as c:
            await c.run_sync(XYOrder.__table__.create)
            await c.run_sync(AutoRateConfig.__table__.create)
        async with self.sessions() as s:
            s.add_all([XYOrder(owner_id=1,account_id='a1',order_no='o1',status='completed'),
                       XYOrder(owner_id=1,account_id='a1',order_no='o2',status='completed'),
                       AutoRateConfig(account_id='a1',enabled=True,text_content='不错的买家',rate_type='text')])
            await s.commit()
        url='/api/v1/auto-rate/tasks/run'
        from common.services.rate_service import RateService
        with patch.object(RateService,'_rate_buyer_impl',new=AsyncMock(side_effect=[TimeoutError(),{'success':False,'message':'明确平台拒绝','definitive_failure':True}]),create=True) as send:
            req={'account_id':'a1','kind':'rate','order_nos':['o1','o2']}
            r=await self.client.post(url,json=req)
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual([i['status'] for i in r.json()['data']],['unknown','failed'])
            repeat=(await self.client.post(url,json=req)).json()['data']
            self.assertEqual([i['reason'] for i in repeat],['unknown','cooldown'])
            self.assertEqual(send.await_count,2)
        history=(await self.client.get('/api/v1/auto-rate/tasks/history',params={'account_id':'a1'})).json()
        self.assertEqual(len(history['data']),4)
        self.user.id=2
        self.assertEqual((await self.client.get('/api/v1/auto-rate/tasks/history',params={'account_id':'a1'})).status_code,404)

    async def test_manual_polish_persists_result_and_unknown_never_replays(self):
        from common.models.xy_catalog_item import XYCatalogItem
        async with self.engine.begin() as conn:
            await conn.run_sync(XYCatalogItem.__table__.create)
        async with self.sessions() as s:
            s.add(XYCatalogItem(owner_id=1,account_pk=1,item_id='i1',title='fixture',created_at=datetime.now(),updated_at=datetime.now()))
            await s.commit()
        self.assertNotEqual((await self.client.get('/api/v1/items/polish/a1/history')).status_code,404)
        from common.models.scheduled_polish_log import ScheduledPolishLog
        async with self.engine.begin() as conn:
            await conn.run_sync(ScheduledPolishLog.__table__.create)
        with patch('common.services.product_polish_service.ProductPolishTransport.send',new=AsyncMock(side_effect=TimeoutError())) as send:
            r=await self.client.post('/api/v1/items/polish/a1')
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.json()['data']['status'],'unknown')
            again=(await self.client.post('/api/v1/items/polish/a1')).json()
            self.assertEqual(again['data']['status'],'already_processed')
            self.assertEqual(send.await_count,1)
        history=(await self.client.get('/api/v1/items/polish/a1/history')).json()['data']
        self.assertEqual(history[0]['status'],'unknown')

    async def test_log_cleanup_keeps_idempotency_and_unknown_evidence(self):
        from common.utils.time_utils import get_beijing_now_naive
        from datetime import timedelta
        async with self.sessions() as s:
            s.add(PublishLog(user_id=1,account_id='a1',title='fixture',status='unknown',
                publish_request_id='keep-after-ten-days',created_at=get_beijing_now_naive()-timedelta(days=20)))
            await s.commit()
        await self.client.delete('/api/v1/product-publish/logs/clear')
        async with self.sessions() as s:
            rows=list((await s.execute(select(PublishLog))).scalars())
            self.assertEqual(len(rows),1)

    async def test_cancel_during_publish_stops_all_not_started_rows(self):
        from common.services.product_batch_service import ProductBatchService
        from common.services import publish_execution_service as execution
        from types import SimpleNamespace
        async with self.sessions() as s:
            bid=(await ProductBatchService(s).create(1,['a1'],[self.material(),self.material()])).id
        async def accepted(**kwargs):
            await self.client.post(f'/api/v1/product-publish/publish/batch/{bid}/cancel')
            return {'success':True,'item_id':'accepted-1'}
        address=SimpleNamespace(apply_to_item_data=lambda d:dict(d),to_log_fields=lambda:{})
        with patch.object(execution,'async_session_maker',self.sessions), \
             patch.object(execution.PublishAddressService,'resolve_publish_address',new=AsyncMock(return_value=address)), \
             patch.object(execution,'detect_publish_account_capability',new=AsyncMock(return_value={'success':True,'is_fish_shop':True})), \
             patch.object(execution,'ensure_publish_capability_reliable',new=lambda r:r), \
             patch.object(execution,'_sync_account_items_after_publish',new=AsyncMock(return_value={})), \
             patch.object(execution,'publish_single_item',new=AsyncMock(side_effect=accepted)) as external:
            async with self.sessions() as s: await ProductBatchService(s).run(1,bid)
            self.assertEqual(external.await_count,1)
        status=(await self.client.get(f'/api/v1/product-publish/publish/batch/{bid}/status')).json()['data']
        self.assertEqual((status['success'],status['cancelled']),(1,1))

    async def test_templates_crud_activation_preserves_switch(self):
        from app.api.routes import auto_rate
        from common.models.auto_rate_config import AutoRateConfig
        self.app.include_router(auto_rate.router,prefix='/api/v1/auto-rate')
        async with self.engine.begin() as c: await c.run_sync(AutoRateConfig.__table__.create)
        async with self.sessions() as s:
            s.add(AutoRateConfig(account_id='a1',enabled=False,thanks_enabled=True,thanks_content='谢谢'))
            await s.commit()
        base='/api/v1/auto-rate/templates/a1'
        first=(await self.client.post(base,json={'name':'模板一','content':'不错的买家'})).json()['data']['id']
        second=(await self.client.post(base,json={'name':'模板二','content':'合作愉快'})).json()['data']['id']
        await self.client.post(f'{base}/{first}/activate')
        await self.client.put(f'{base}/{first}',json={'name':'修改','content':'修改后的内容'})
        async with self.sessions() as s:
            cfg=(await s.execute(select(AutoRateConfig))).scalar_one()
            self.assertFalse(cfg.enabled); self.assertTrue(cfg.thanks_enabled); self.assertEqual(cfg.text_content,'修改后的内容')
        self.assertEqual((await self.client.delete(f'{base}/{first}')).status_code,409)
        await self.client.post(f'{base}/{second}/activate')
        await self.client.delete(f'{base}/{first}')
        rows=(await self.client.get(base)).json()['data']
        self.assertEqual(len(rows),1);self.assertTrue(rows[0]['active'])
        self.user.id=2
        self.assertEqual((await self.client.get(base)).status_code,404)

    async def test_polish_recovery_after_window_is_skipped_not_caught_up(self):
        from common.services.product_polish_service import ProductPolishService
        from common.services.product_polish_schedule import ProductPolishScheduleService
        from common.models.xy_account import XYAccount
        from datetime import timezone
        async with self.sessions() as s:
            account=await s.get(XYAccount,1);account.auto_polish=True
            await ProductPolishScheduleService(s).save(1,'a1',dict(timezone_name='UTC',start='09:00',end='10:00'))
            account.metadata_json={'xy_runtime':{'business_state':'recovering'}}; await s.commit()
            first=await ProductPolishService(s).run(account,'scheduled',now=datetime(2026,9,21,9,30,tzinfo=timezone.utc))
            self.assertEqual(first['status'],'recovering')
            account.metadata_json={};await s.commit()
            with patch('common.services.product_polish_service.ProductPolishTransport.send',new=AsyncMock()) as send:
                second=await ProductPolishService(s).run(account,'scheduled',now=datetime(2026,9,21,10,30,tzinfo=timezone.utc))
            self.assertEqual(second['status'],'outside_window');send.assert_not_awaited()
            schedule=await ProductPolishScheduleService(s).get(1,'a1')
            self.assertIsNone(schedule.last_cycle);self.assertEqual(schedule.last_status,'skipped_outside_window')

    async def test_batch_history_restores_after_browser_session_loss(self):
        from common.services.product_batch_service import ProductBatchService
        async with self.sessions() as s:
            bid=(await ProductBatchService(s).create(1,['a1'],[self.material()])).id
        response=await self.client.get('/api/v1/product-publish/publish/batches')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['data'][0]['batch_id'],bid)

    async def test_local_delete_preserves_other_owner_relations_and_records_action(self):
        from common.models.xy_catalog_item import XYCatalogItem
        from common.models.card_item_relation import CardItemRelation
        async with self.engine.begin() as c:
            await c.run_sync(XYCatalogItem.__table__.create)
            await c.run_sync(CardItemRelation.__table__.create)
        async with self.sessions() as s:
            for i in (1,2):
                s.add(XYCatalogItem(owner_id=i,account_pk=i,item_id='same-id',title='fixture',created_at=datetime.now(),updated_at=datetime.now()))
                s.add(CardItemRelation(user_id=i,card_id=i,item_id='same-id'))
            await s.commit()
        response=await self.client.delete('/api/v1/items/a1/same-id')
        self.assertTrue(response.json()['success'],response.text)
        async with self.sessions() as s:
            rows=list((await s.execute(select(CardItemRelation))).scalars())
            self.assertEqual([r.user_id for r in rows],[2])
        history=await self.client.get('/api/v1/items/operations/a1/history')
        self.assertEqual(history.status_code,200,history.text)
        self.assertEqual(history.json()['data'][0]['action'],'local_delete')

    async def test_queued_material_resolves_and_freezes_account_address(self):
        from common.services.product_batch_service import ProductBatchService
        from common.services import publish_execution_service as execution
        from types import SimpleNamespace
        async with self.sessions() as s:
            bid=(await ProductBatchService(s).create(1,['a1'],[self.material()])).id
        address=SimpleNamespace(apply_to_item_data=lambda d:{**d,'address':'resolved pool address'},to_log_fields=lambda:{'resolved_address_text':'resolved pool address','address_source':'account_pool'})
        external=AsyncMock(return_value={'success':True,'item_id':'platform-item'})
        async def syncing(**kwargs):
            status=(await self.client.get(f'/api/v1/product-publish/publish/batch/{bid}/status')).json()['data']
            self.assertFalse(status['finished'], '发布后的同步仍在执行，批次尚未结束')
            return {'sync_status':'success','sync_saved_count':3,'sync_total_count':3,'sync_message':'同步完成'}
        with patch.object(execution,'async_session_maker',self.sessions), \
             patch.object(execution.PublishAddressService,'resolve_publish_address',new=AsyncMock(return_value=address)), \
             patch.object(execution,'detect_publish_account_capability',new=AsyncMock(return_value={'success':True,'is_fish_shop':True})), \
             patch.object(execution,'ensure_publish_capability_reliable',new=lambda r:r), \
             patch.object(execution,'_sync_account_items_after_publish',new=AsyncMock(side_effect=syncing)), \
             patch.object(execution,'publish_single_item',new=external):
            async with self.sessions() as s: await ProductBatchService(s).run(1,bid)
        self.assertEqual(external.call_args.kwargs['item_data'].get('address'),'resolved pool address')
        async with self.sessions() as s:
            row=(await s.execute(select(PublishLog))).scalar_one()
            self.assertEqual(row.publish_snapshot['address'],'resolved pool address')

        status=(await self.client.get(f'/api/v1/product-publish/publish/batch/{bid}/status')).json()['data']
        self.assertEqual(status['account_statuses'][0]['sync_status'],'success')

    async def test_reconcile_published_checks_scoped_catalog_and_sets_link(self):
        from common.models.xy_catalog_item import XYCatalogItem
        from common.services.publish_log_service import PublishLogService
        async with self.engine.begin() as c: await c.run_sync(XYCatalogItem.__table__.create)
        async with self.sessions() as s:
            for i in (1,2):
                s.add(XYCatalogItem(owner_id=i,account_pk=i,item_id=f'item{i}',title='fixture',created_at=datetime.now(),updated_at=datetime.now()))
            await s.commit()
            lid=(await PublishLogService(s).create_log(user_id=1,account_id='a1',title='fixture',status='unknown')).id
        url=f'/api/v1/product-publish/logs/{lid}/reconcile'
        self.assertFalse((await self.client.post(url,json={'outcome':'published','item_id':'item2','evidence':'人工核对账号商品证据'})).json()['success'])
        self.assertTrue((await self.client.post(url,json={'outcome':'published','item_id':'item1','evidence':'人工核对账号商品证据'})).json()['success'])
        async with self.sessions() as s:
            row=await s.get(PublishLog,lid)
            self.assertEqual(row.status,'success');self.assertIn('item1',row.item_url or '')

    async def test_fresh_publishing_log_is_not_manually_reconciled(self):
        from common.services.publish_log_service import PublishLogService
        async with self.sessions() as s:
            lid=(await PublishLogService(s).create_log(user_id=1,account_id='a1',title='fixture',status='publishing')).id
        response=(await self.client.post(f'/api/v1/product-publish/logs/{lid}/reconcile',json={'outcome':'not_published','evidence':'仍在执行时的核对请求'})).json()
        self.assertFalse(response['success'],response)
