import unittest
from unittest.mock import AsyncMock, patch
import importlib.util
import test_fulfillment as fixtures
from common.services.order_service import OrderService, OrderStatusChecker

class OrderGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self): await fixtures.FulfillmentTests.asyncSetUp(self)
    async def asyncTearDown(self): await self.engine.dispose()
    async def test_order_read_gateway_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('common.services.order_platform_gateway'))
    async def test_old_order_service_uses_executor_not_local_aiohttp(self):
        from common.services import order_platform_gateway as gateway
        call=AsyncMock(return_value={'data':{'module':{'items':[],'nextPage':'false','totalCount':'0'}},'ret':['SUCCESS']})
        with patch.object(gateway,'read_order_platform',call),patch('aiohttp.ClientSession',side_effect=AssertionError('web禁止直连平台')):
            async with self.sessions() as s:
                result=await OrderService(s)._fetch_sold_orders_page('DO_NOT_SEND',1,account_id='a')
        self.assertEqual(result['items'],[]);call.assert_awaited_once()
        self.assertNotIn('DO_NOT_SEND',str(call.call_args))
    async def test_legacy_checker_uses_account_scoped_rpc(self):
        from common.services import order_platform_gateway as gateway
        call=AsyncMock(return_value={'data':{},'ret':['SUCCESS']})
        with patch.object(gateway,'read_order_platform',call),patch('aiohttp.ClientSession',side_effect=AssertionError('scheduler禁止直连平台')):
            await OrderStatusChecker('DO_NOT_SEND','a')._fetch_raw_order_detail('same')
        self.assertEqual(call.call_args.args,('a','detail'))
        self.assertEqual(call.call_args.kwargs['order_no'],'same')
    async def test_detail_multiline_response_persists_all_skus_instead_of_last_one(self):
        from common.services.order_service import OrderDetailService
        from common.services.order_lines import order_lines
        from common.models.xy_order import XYOrder
        payload={'data':{'components':[
            {'render':'orderInfoVO','data':{'subOrderId':'red','itemInfo':{'itemId':'item','title':'卡券','buyAmount':'2','totalPrice':'20.00','skuInfo':'颜色:红'}}},
            {'render':'orderInfoVO','data':{'subOrderId':'blue','itemInfo':{'itemId':'item','title':'卡券','buyAmount':'1','totalPrice':'11.00','skuInfo':'颜色:蓝'}}},
        ]}}
        with patch('common.services.order_platform_gateway.read_order_platform',AsyncMock(return_value=payload)),patch('common.db.session.async_session_maker',self.sessions):
            self.assertTrue(await OrderDetailService('a','fixture').fetch_and_update_order_detail('same'))
        async with self.sessions() as session:
            order=await session.get(XYOrder,1)
            self.assertEqual(order.quantity,3)
            self.assertEqual(str(order.amount),'31.00')
            self.assertEqual({line.line_id:(line.spec_value,line.quantity,line.amount) for line in order_lines(order)}, {'red':('红',2,'20.00'),'blue':('蓝',1,'11.00')})

    async def test_ambiguous_multiple_detail_components_block_fulfillment(self):
        from common.services.order_service import OrderDetailService
        from common.services.order_lines import order_lines
        from common.models.xy_order import XYOrder
        payload={'data':{'components':[{'render':'orderInfoVO','data':{'itemInfo':{'itemId':'item','buyAmount':'1','skuInfo':sku}}} for sku in ('颜色:红','颜色:蓝')]}}
        with patch('common.services.order_platform_gateway.read_order_platform',AsyncMock(return_value=payload)),patch('common.db.session.async_session_maker',self.sessions):
            await OrderDetailService('a','fixture').fetch_and_update_order_detail('same')
        async with self.sessions() as session:
            with self.assertRaisesRegex(ValueError,'明细'):
                order_lines(await session.get(XYOrder,1))
