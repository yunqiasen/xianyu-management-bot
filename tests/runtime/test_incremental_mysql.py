"""S3: an actual MySQL upgrade preserves existing filters and daily plans."""
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from sqlalchemy import inspect, insert, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from common.db.fork_schema import upgrade_fork_schema
from common.models.reply_state import advanced_filters
from common.services.reply_state import ReplyState
from common.services.product_polish_schedule import ProductPolishScheduleService
from tools.verification.run import commerce_fixture


class IncrementalMySQLTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_preserves_legacy_rules_and_random_plan_across_workers(self):
        with commerce_fixture() as url:
            engine=create_async_engine(url,hide_parameters=True)
            sessions=async_sessionmaker(engine,expire_on_commit=False)
            try:
                async with engine.begin() as connection:
                    await connection.execute(text("""CREATE TABLE xy_reply_filters (
                        id VARCHAR(64) PRIMARY KEY,account_id VARCHAR(80) NOT NULL,
                        pattern VARCHAR(256) NOT NULL,match_mode VARCHAR(20) NOT NULL,
                        source VARCHAR(20) NOT NULL,item_id VARCHAR(64) NOT NULL,
                        actions JSON NOT NULL,enabled BOOLEAN NOT NULL,version INTEGER NOT NULL)"""))
                    await connection.execute(text("""INSERT INTO xy_reply_filters VALUES
                        ('old','account','hold','contains','user','',JSON_ARRAY('pause'),1,9)"""))
                    await connection.execute(text("""CREATE TABLE xy_product_polish_schedules (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,owner_id BIGINT NOT NULL,
                        account_id VARCHAR(80) NOT NULL,timezone_name VARCHAR(64) NOT NULL,
                        start VARCHAR(5) NOT NULL,end VARCHAR(5) NOT NULL,last_cycle VARCHAR(100),
                        last_status VARCHAR(40),created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_product_polish_account(owner_id,account_id))"""))
                    await connection.execute(text("""INSERT INTO xy_product_polish_schedules
                        (owner_id,account_id,timezone_name,start,end,last_cycle,last_status)
                        VALUES(7,'account','UTC','09:00','10:00','UTC:2026-09-22','claimed')"""))
                    await connection.run_sync(upgrade_fork_schema)
                    await connection.run_sync(upgrade_fork_schema)
                    indexes=await connection.run_sync(lambda conn:inspect(conn).get_indexes('xy_reply_filters'))
                    self.assertIn(['owner_id'],[index['column_names'] for index in indexes])
                state=ReplyState(sessions)
                before=await state.filters('account',owner_id=7)
                self.assertEqual([(r['id'],r['pattern'],r['version'],r['pause_minutes']) for r in before],
                                 [('old','hold',9,None)])
                async with sessions() as session:
                    await session.execute(insert(advanced_filters).values(id='global',owner_id=7,account_id='',
                        pattern='shared',match_mode='contains',source='user',actions=['pause'],pause_minutes=17))
                    await session.commit()
                self.assertEqual([r['id'] for r in await state.filters('future',owner_id=7)],['global'])
                self.assertEqual(await state.filters('future',owner_id=8),[])
                async with sessions() as session:
                    service=ProductPolishScheduleService(session)
                    row=await service.get(7,'account')
                    self.assertFalse(row.randomize)
                    self.assertEqual(row.last_cycle,'UTC:2026-09-22')
                    await service.save(7,'account',{'timezone_name':'UTC','start':'09:00','end':'10:00','randomize':True})
                account=SimpleNamespace(owner_id=7,account_id='account',status='active',auto_polish=True)
                now=datetime(2026,9,23,9,0,tzinfo=timezone.utc)
                async def admit(at):
                    async with sessions() as session:
                        return await ProductPolishScheduleService(session).admit(account,at)
                with patch('secrets.randbelow',return_value=23) as random:
                    first=await admit(now)
                    self.assertEqual(first['status'],'waiting')
                    again=await admit(now+timedelta(minutes=10))
                    self.assertEqual(first['planned_at'],again['planned_at'])
                    results=await asyncio.gather(admit(now+timedelta(minutes=24)),admit(now+timedelta(minutes=24)))
                    self.assertCountEqual([r['status'] for r in results],['ready','already_processed'])
                    random.assert_called_once_with(60)
            finally:
                await engine.dispose()
