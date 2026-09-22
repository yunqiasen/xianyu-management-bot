"""S1 HTTP -> 身份校验 -> 真实订单/库存/检查点；平台由本地桩替换。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import httpx
import test_fulfillment as fixtures
from fastapi import FastAPI
from app.api import deps
from app.api.routes.order_commerce import router
from common.models.order_sync_job import OrderSyncJob
from common.models.card_item_relation import CardItemRelation
from common.services.order_history import OrderHistory

class CommerceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.FulfillmentTests.asyncSetUp(self)
        async with self.engine.begin() as c:
            for model in (OrderSyncJob,CardItemRelation):await c.run_sync(model.__table__.create)
        self.user=SimpleNamespace(id=1, is_superuser=False, role='user')
        async def db():
            async with self.sessions() as s:yield s
        app=FastAPI();self.app=app;app.include_router(router,prefix='/api/v1/orders')
        app.dependency_overrides[deps.get_db_session]=db
        app.dependency_overrides[deps.get_current_active_user]=lambda:self.user
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture')
    async def asyncTearDown(self):await self.client.aclose();await self.engine.dispose()
    async def test_history_http_create_step_cancel_and_resume(self):
        base='/api/v1/orders/commerce/history'
        response=await self.client.post(base,json={'account_id':'a'});self.assertEqual(response.status_code,200,response.text)
        job=response.json()['data']['id']
        with patch.object(OrderHistory,'fetch_platform_page',AsyncMock(return_value={'orders':[],'has_next':True,'total_pages':2})):
            response=await self.client.post(f'{base}/{job}/step')
        self.assertEqual(response.json()['data']['next_page'],2)
        self.assertEqual((await self.client.post(f'{base}/{job}/cancel')).json()['data']['status'],'cancelled')
        self.assertEqual((await self.client.post(f'{base}/{job}/resume')).json()['data']['next_page'],2)
        self.user.id=2
        self.assertEqual((await self.client.post(f'{base}/{job}/step')).status_code,404)
    async def test_intent_list_masks_content_and_detail_checks_owner(self):
        row=await self.service.reserve(1,'a','same',1)
        base='/api/v1/orders/commerce/intents'
        response=await self.client.get(base)
        self.assertNotIn('payload',response.json()['data'][0])
        response=await self.client.get(f'{base}/{row.id}/content');self.assertEqual(response.json()['data']['texts'],['A','B'])
        self.user.id=2
        self.assertEqual((await self.client.get(f'{base}/{row.id}/content')).status_code,404)
        self.assertEqual((await self.client.post(f'{base}/{row.id}/confirm')).status_code,404)
    async def test_confirm_endpoint_never_dispatches_unknown_content(self):
        row=await self.service.reserve(1,'a','same',1)
        with patch('app.api.routes.order_commerce.dispatch',AsyncMock()) as dispatch:
            response=await self.client.post(f'/api/v1/orders/commerce/intents/{row.id}/confirm')
            self.assertEqual(response.status_code,409);dispatch.assert_not_called()
    async def test_supplier_manual_evidence_is_owned_and_quantity_checked(self):
        from common.models.card import Card
        async with self.sessions() as s:
            card=await s.get(Card,1);card.type='api';card.api_config='{}';await s.commit()
        row=await self.service.reserve(1,'a','same',1)
        await self.service.execute(row.id,1,send=None,confirm=None,purchase=AsyncMock(side_effect=TimeoutError))
        url=f'/api/v1/orders/commerce/intents/{row.id}/supplier-evidence'
        self.user.id=2
        self.assertEqual((await self.client.post(url,json={'texts':['X','Y'],'evidence':'receipt:123'})).status_code,404)
        self.user.id=1
        self.assertEqual((await self.client.post(url,json={'texts':['X'],'evidence':'receipt:123'})).status_code,409)
        result=await self.client.post(url,json={'texts':['X','Y'],'evidence':'receipt:123'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['data']['content_state'],'reserved')
        listed=(await self.client.get('/api/v1/orders/commerce/intents')).json()['data'][0]
        self.assertNotIn('api_config',str(listed));self.assertIn('source',listed)
