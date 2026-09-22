import unittest
from unittest.mock import AsyncMock
from sqlalchemy import BigInteger,select,func
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.models.order_sync_job import OrderSyncJob
from common.services.order_history import OrderHistory

@compiles(BigInteger,'sqlite')
def bigint(*args,**kw): return 'INTEGER'
class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine=create_async_engine('sqlite+aiosqlite:///:memory:'); self.sessions=async_sessionmaker(self.engine,expire_on_commit=False)
        async with self.engine.begin() as c:
            for m in (XYAccount,XYOrder,OrderSyncJob): await c.run_sync(m.__table__.create)
        async with self.sessions() as s:
            s.add(XYAccount(id=1,owner_id=1,account_id='a',cookie='fixture',unb='1',login_method='cookie',status='active'));await s.commit()
        self.service=OrderHistory(self.sessions)
    async def asyncTearDown(self): await self.engine.dispose()

    async def test_cancel_resume_checkpoints_and_duplicate_import(self):
        job=await self.service.create(1,'a')
        fetch=AsyncMock(return_value={'orders':[{'order_no':'o','status':'pending_ship'}],'has_next':True,'total_pages':2})
        job=await self.service.step(1,job.id,fetch=fetch); self.assertEqual(job.next_page,2)
        await self.service.cancel(1,job.id)
        await self.service.step(1,job.id,fetch=fetch); self.assertEqual(fetch.call_count,1)
        await self.service.resume(1,job.id)
        fetch.return_value={'orders':[{'order_no':'o','status':'shipped'}],'has_next':False,'total_pages':2}
        job=await self.service.step(1,job.id,fetch=fetch)
        self.assertEqual(fetch.call_args.args[1],2); self.assertEqual(job.status,'completed')
        async with self.sessions() as s:
            self.assertEqual(await s.scalar(select(func.count()).select_from(XYOrder)),1)
            self.assertEqual((await s.scalar(select(XYOrder))).status,'shipped')

    async def test_cancel_in_flight_discards_page(self):
        job=await self.service.create(1,'a')
        async def fetch(account,page):
            await self.service.cancel(1,job.id)
            return {'orders':[{'order_no':'o','status':'pending_ship'}],'has_next':False}
        result=await self.service.step(1,job.id,fetch=fetch)
        self.assertEqual(result.status,'cancelled'); self.assertEqual(result.next_page,1)
        async with self.sessions() as s: self.assertEqual(await s.scalar(select(func.count()).select_from(XYOrder)),0)

    async def test_failed_page_is_resumable_and_errors_redacted(self):
        job=await self.service.create(1,'a')
        result=await self.service.step(1,job.id,fetch=AsyncMock(side_effect=TimeoutError('TOKEN=secret')))
        self.assertEqual(result.status,'paused'); self.assertEqual(result.next_page,1)
        self.assertNotIn('secret',result.last_error)
        with self.assertRaises(PermissionError): await self.service.resume(2,job.id)

    async def test_history_preserves_multisku_lines_and_quantity_price_identity(self):
        job=await self.service.create(1,'a')
        lines=[{'line_id':'blue','item_id':'i','spec_name':'颜色','spec_value':'蓝','quantity':1,'amount':'12.00'},
               {'line_id':'red','item_id':'i','spec_name':'颜色','spec_value':'红','quantity':2,'amount':'20.00'}]
        fetch=AsyncMock(return_value={'orders':[{'order_no':'with-lines','item_id':'i','status':'pending_ship','quantity':3,'order_lines':lines}], 'has_next':False})
        await self.service.step(1,job.id,fetch=fetch)
        async with self.sessions() as session:
            row=await session.scalar(select(XYOrder).where(XYOrder.order_no=='with-lines'))
            from common.services.order_lines import order_lines
            self.assertEqual([(line.line_id,line.quantity,line.amount) for line in order_lines(row)], [('blue',1,'12.00'),('red',2,'20.00')])
