"""真实隔离 MySQL 验证抢最后库存；仅连接明确标记的空夹具库。"""
import asyncio
import os
import unittest
from uuid import uuid4
from sqlalchemy import select,func
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from common.models.card import Card
from common.models.xy_order import XYOrder
from common.models.xy_account import XYAccount
from common.models.delivery_intent import DeliveryIntent
from common.services.delivery_execution import DeliveryExecution

class MySQLInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url=os.environ.get('XYMB_COMMERCE_MYSQL_URL')
        if not url:self.skipTest('XYMB_COMMERCE_MYSQL_URL 未配置：仅隔离库执行')
        parsed=make_url(url)
        if parsed.host not in {'127.0.0.1','localhost'} or not (parsed.database or '').startswith('xy_commerce_fixture_'):
            raise ValueError('仅允许本机专用 commerce fixture 数据库')
        self.engine=create_async_engine(url);self.sessions=async_sessionmaker(self.engine,expire_on_commit=False)
        async with self.engine.begin() as c:
            for model in (XYAccount,XYOrder,Card,DeliveryIntent):await c.run_sync(lambda sync,m=model:m.__table__.create(sync,checkfirst=True))
        self.identity='test-'+uuid4().hex
        async with self.sessions() as s:
            account=XYAccount(owner_id=777,account_id=self.identity,unb=self.identity,cookie='fixture',login_method='cookie',status='active')
            card=Card(user_id=777,name='fixture',type='data',data_content='LAST-CARD')
            s.add_all([account,card]);await s.flush();self.card_id=card.id
            s.add_all([XYOrder(owner_id=777,account_id=self.identity,order_no=str(i),status='pending_ship',quantity=1) for i in range(2)])
            await s.commit()
    async def asyncTearDown(self):
        if hasattr(self,'engine'):await self.engine.dispose()
    async def test_two_orders_compete_for_last_card(self):
        service=DeliveryExecution(self.sessions)
        barrier=asyncio.Event()
        async def reserve(number):
            await barrier.wait()
            try:return await service.reserve(777,self.identity,str(number),self.card_id)
            except ValueError:return None
        tasks=[asyncio.create_task(reserve(i)) for i in range(2)];barrier.set()
        results=await asyncio.gather(*tasks)
        self.assertEqual(sum(r is not None for r in results),1)
        async with self.sessions() as s:
            self.assertEqual((await s.get(Card,self.card_id)).data_content,'')
            count=await s.scalar(select(func.count()).select_from(DeliveryIntent).where(DeliveryIntent.account_id==self.identity))
            self.assertEqual(count,1)

    async def test_duplicate_multiline_orders_reserve_each_sku_once(self):
        from unittest.mock import AsyncMock
        from common.services.order_delivery_runtime import OrderDeliveryRuntime
        async with self.sessions() as session:
            red=await session.get(Card,self.card_id); red.data_content='R1\nR2\nR3'; red.is_multi_spec=True; red.spec_name='颜色'; red.spec_value='红'
            blue=Card(user_id=777,name='蓝',type='data',data_content='B1\nB2',is_multi_spec=True,spec_name='颜色',spec_value='蓝')
            session.add(blue); await session.flush(); blue_id=blue.id
            order=await session.scalar(select(XYOrder).where(XYOrder.account_id==self.identity,XYOrder.order_no=='0'))
            order.quantity=3;order.metadata_json={'order_lines':[
                {'line_id':'red','quantity':2,'item_id':'item','spec_name':'颜色','spec_value':'红','amount':'20.00'},
                {'line_id':'blue','quantity':1,'item_id':'item','spec_name':'颜色','spec_value':'蓝','amount':'11.00'}]}
            await session.commit()
        choices=[{'line_id':'red','card_id':self.card_id},{'line_id':'blue','card_id':blue_id}]
        a,b=await asyncio.gather(*(DeliveryExecution(self.sessions).reserve_batch(777,self.identity,'0',choices) for _ in range(2)))
        self.assertEqual({r.id for r in a},{r.id for r in b})
        send=AsyncMock(return_value={'delivery_state':'confirmed'});confirm=AsyncMock(return_value='confirmed')
        await asyncio.gather(*(OrderDeliveryRuntime(self.sessions).execute_group(rows,send_text=send,send_image=send,confirm=confirm) for rows in (a,b)))
        self.assertCountEqual([c.args[0] for c in send.call_args_list],['R1','R2','B1'])
        self.assertEqual(confirm.call_count,1)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Card,self.card_id)).data_content,'R3')
            self.assertEqual((await session.get(Card,blue_id)).data_content,'B2')
