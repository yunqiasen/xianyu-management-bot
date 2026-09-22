"""旧内部API桥接：真实SQL/履约，只有平台通信使用桩。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import test_runtime as fixtures
from sqlalchemy import select
from common.models.card import Card
from common.models.xy_order import XYOrder
from common.models.delivery_intent import DeliveryIntent
from common.services.delivery_execution import DeliveryExecution

class LegacyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.RuntimeTests.asyncSetUp(self)
        self.handler=SimpleNamespace(send_msg=AsyncMock(return_value={'delivery_state':'confirmed'}),send_image_msg=AsyncMock(return_value={'delivery_state':'confirmed'}),
            auto_confirm=AsyncMock(return_value={'success':True}),auto_freeshipping=AsyncMock(return_value={'success':True}),
            pre_delivery_check_and_close=AsyncMock(return_value={'action':'allow'}),_ensure_order_amount_before_delivery=AsyncMock(return_value=True),
            is_auto_confirm_enabled=lambda:True,is_only_send_card_enabled=lambda:False,is_confirm_before_send_enabled=lambda:False,
            is_send_before_confirm_enabled=lambda:True,get_agree_deliver_config=lambda:{})
        self.live=SimpleNamespace(ws=object(),auto_delivery_handler=self.handler,_check_account_execution=AsyncMock(),cookie_id='a')
        self.request=SimpleNamespace(owner_id=1,account_id='a',order_no='same',item_id='item',buyer_id='buyer',chat_id='chat',card_id=1,quantity=999,is_bargain=False,delivery_method='manual')
    async def asyncTearDown(self): await self.engine.dispose()
    async def call(self):
        from app.services.shipping import legacy_bridge
        with patch.object(legacy_bridge,'async_session_maker',self.sessions),patch('app.services.xianyu.cookie_manager.get_manager',return_value=SimpleNamespace(instances={'a':self.live})),patch('common.db.compat.db_manager.get_item_info',return_value={'item_id':'item'}):
            return await legacy_bridge.maybe_deliver(self.request)
    async def test_bridge_exists(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('app.services.shipping.legacy_bridge'),'旧发货入口尚未接持久履约')
    async def test_quantity_from_order_and_replay_only_confirms(self):
        self.handler.auto_confirm.return_value={'success':False,'outcome':'not_sent'}
        first=await self.call()
        self.assertFalse(first['success'])
        self.assertEqual(first['data']['content_state'],'confirmed')
        self.handler.auto_confirm.return_value={'success':True}
        async with self.sessions() as s:
            row=await s.get(DeliveryIntent,first['data']['id']);row.next_retry_at=0;await s.commit()
        second=await self.call()
        self.assertTrue(second['success']);self.assertEqual(self.handler.send_msg.await_count,2)
        async with self.sessions() as s:self.assertEqual((await s.get(Card,1)).data_content,'C')
    async def test_wrong_buyer_rejected_before_precheck(self):
        self.request.buyer_id='other';result=await self.call()
        self.assertEqual(result['code'],409);self.handler.pre_delivery_check_and_close.assert_not_called()
    async def test_legacy_ambiguous_order_rejected(self):
        self.request.owner_id=None;self.request.account_id=None
        async with self.sessions() as s:
            s.add(XYOrder(owner_id=2,account_id='b',order_no='same',item_id='item',status='pending_ship'));await s.commit()
        result=await self.call();self.assertEqual(result['code'],409);self.handler.send_msg.assert_not_called()
    async def test_manual_selection_retained_and_wrong_item_rejected(self):
        async with self.sessions() as s:
            s.add(Card(id=3,user_id=1,item_id='item',name='selected',type='data',data_content='D\nE'))
            s.add(Card(id=4,user_id=1,item_id='other',name='wrong',type='data',data_content='F\nG'));await s.commit()
        self.request.card_id=4;self.assertEqual((await self.call())['code'],409)
        self.request.card_id=3;self.assertTrue((await self.call())['success'])
        self.assertEqual([a.args[-1] for a in self.handler.send_msg.await_args_list],['D','E'])
    async def test_unknown_intent_blocks_special_chain_and_repurchase(self):
        row=await self.service.reserve(1,'a','same',1)
        await self.service.execute(row.id,1,send=AsyncMock(return_value='unknown'),confirm=None)
        self.handler.get_agree_deliver_config=lambda:{'enabled':True,'pickup_url':'fixture'}
        result=await self.call();self.assertIsNotNone(result);self.assertFalse(result['success']);self.handler.send_msg.assert_not_called()
    async def test_precheck_block_retains_inventory(self):
        self.handler.pre_delivery_check_and_close.return_value={'action':'block'}
        result=await self.call();self.assertFalse(result['success'])
        async with self.sessions() as s:self.assertEqual((await s.get(Card,1)).data_content,'A\nB\nC')

    async def test_start_request_keeps_card_selection(self):
        from app.services.shipping.commerce_routes import StartRequest
        self.assertEqual(getattr(StartRequest(owner_id=1,account_id='a',order_no='same',card_id=3),'card_id',None),3)

    async def test_foreign_card_is_not_special_escape(self):
        async with self.sessions() as s:
            card=await s.get(Card,1);card.item_id='other'
            s.add(Card(id=4,user_id=2,item_id='item',name='foreign',type='data',data_content='F'));await s.commit()
        self.request.card_id=4
        result=await self.call();self.assertIsNotNone(result);self.assertEqual(result['code'],409)

    async def test_bargain_confirm_steps_do_not_repeat_successful_freeshipping(self):
        async with self.sessions() as s:
            order=await s.get(XYOrder,1);order.is_bargain=True;await s.commit()
        self.handler.auto_confirm.return_value={'success':False,'outcome':'not_sent'}
        result=await self.call()
        async with self.sessions() as s:
            row=await s.get(DeliveryIntent,result['data']['id']);row.next_retry_at=0;await s.commit()
        self.handler.auto_confirm.return_value={'success':True}
        self.assertTrue((await self.call())['success'])
        self.handler.auto_freeshipping.assert_awaited_once()
        self.assertEqual(self.handler.send_msg.await_count,2)

    async def test_image_card_bridge_and_replay(self):
        async with self.sessions() as s:
            card=await s.get(Card,1);card.type='image';card.image_url='https://fixture.invalid/card.png';await s.commit()
        self.assertTrue((await self.call())['success'])
        self.assertTrue((await self.call())['success'])
        self.handler.send_image_msg.assert_awaited_once();self.handler.send_msg.assert_not_called()

    async def test_api_unknown_local_supplier_query_then_send_no_repurchase(self):
        import aiohttp
        from aiohttp import web
        from app.services.shipping import commerce_routes
        buys=[];queries=[]
        async def buy(request):
            buys.append(request.headers['Idempotency-Key'])
            return web.Response(status=504)
        async def query(request):
            queries.append(request.headers['Idempotency-Key'])
            return web.json_response({'data':'REMOTE-A\nREMOTE-B'})
        app=web.Application();app.router.add_get('/buy',buy);app.router.add_get('/query',query)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        base='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
        self.live._build_session_connector=lambda:aiohttp.TCPConnector()
        try:
            import json
            async with self.sessions() as s:
                card=await s.get(Card,1);card.type='api';card.api_config=json.dumps({'url':base+'/buy','query_url':base+'/query','response_field':'data','idempotency_header':'Idempotency-Key'});await s.commit()
            first=await self.call();self.assertEqual(first['data']['content_state'],'unknown')
            await self.call();self.assertEqual(len(buys),1)
            with patch.object(commerce_routes,'async_session_maker',self.sessions),patch('app.services.xianyu.cookie_manager.get_manager',return_value=SimpleNamespace(instances={'a':self.live})):
                await commerce_routes.advance_fulfillment(commerce_routes.AdvanceRequest(owner_id=1,intent_id=first['data']['id'],query_supplier=True))
            self.assertTrue((await self.call())['success'])
            self.assertEqual(buys,queries);self.assertEqual(len(buys),1)
            self.assertEqual([a.args[-1] for a in self.handler.send_msg.await_args_list],['REMOTE-A','REMOTE-B'])
        finally: await runner.cleanup()

    async def test_owned_dock_relation_preserves_original_special_route(self):
        from common.models.card_item_relation import CardItemRelation
        async with self.sessions() as s:
            card=await s.get(Card,1);card.item_id='other'
            s.add(Card(id=4,user_id=2,item_id='foreign-item',name='dock',type='api',api_config='{}'))
            s.add(CardItemRelation(user_id=1,card_id=4,item_id='item',source='dock_l1',dock_record_id=1));await s.commit()
        self.request.card_id=None
        self.assertIsNone(await self.call())
        self.handler.send_msg.assert_not_called()

    async def test_existing_internal_entry_handles_complete_multisku_order(self):
        self.request.card_id=None
        async with self.sessions() as session:
            red=await session.get(Card,1);red.is_multi_spec=True;red.spec_name='颜色';red.spec_value='红'
            session.add(Card(id=2,user_id=1,item_id='item',name='blue',type='data',data_content='BLUE',
                             is_multi_spec=True,spec_name='颜色',spec_value='蓝'))
            order=await session.get(XYOrder,1);order.quantity=3
            order.metadata_json={'order_lines':[
                {'line_id':'red','item_id':'item','spec_name':'颜色','spec_value':'红','quantity':2},
                {'line_id':'blue','item_id':'item','spec_name':'颜色','spec_value':'蓝','quantity':1}]}
            await session.commit()
        result=await self.call()
        self.assertTrue(result['success'],result)
        self.assertCountEqual([call.args[-1] for call in self.handler.send_msg.await_args_list],['A','B','BLUE'])
        self.assertEqual(result['data']['quantity_sent'],3)
        self.assertTrue((await self.call())['success'])
        self.assertEqual(self.handler.auto_confirm.await_count,1)
        self.assertEqual(self.handler.send_msg.await_count,3)

if __name__=='__main__':unittest.main()
