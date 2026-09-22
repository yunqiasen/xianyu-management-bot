"""S1/S2: real MySQL row locks and Redis Lua, only in an explicit isolated DB.

Set XYMB_INTEGRATION_ENV to the private compose environment. Fixture records
have unique identities and are deleted individually; no global flush/truncate.
"""
import asyncio
import os
from pathlib import Path
import unittest
import uuid
from sqlalchemy import delete, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import redis.asyncio as redis
from common.db.fork_schema import upgrade_fork_schema
from common.models import XYAccount, XYOrder, Card, DeliveryIntent
from common.models.reply_state import TABLES as REPLY_TABLES
from common.services.account_execution import AccountLease, ExecutionLost
from common.services.delivery_execution import DeliveryExecution
from common.services.reply_state import ReplyState

@unittest.skipUnless(os.getenv('XYMB_INTEGRATION_ENV'), 'explicit isolated infrastructure required')
class MySQLRedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        values = dict(line.split('=', 1) for line in Path(os.environ['XYMB_INTEGRATION_ENV']).read_text().splitlines() if '=' in line)
        if values['MYSQL_HOST'] != '127.0.0.1' or values['MYSQL_PORT'] != '19006' or values['MYSQL_DATABASE'] != 'xymb_integration':
            raise RuntimeError('expected xymb isolated compose target')
        url = URL.create('mysql+asyncmy', username=values['MYSQL_USER'], password=values['MYSQL_PASSWORD'], host=values['MYSQL_HOST'], port=int(values['MYSQL_PORT']), database=values['MYSQL_DATABASE'])
        self.engine = create_async_engine(url, pool_size=8, max_overflow=0)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.redis = redis.Redis(host='127.0.0.1', port=19379, decode_responses=True)
        self.account = 'contract-' + uuid.uuid4().hex
        self.owner = 920_000_000 + (uuid.uuid4().int % 50_000_000)
        async with self.engine.begin() as conn:
            await conn.run_sync(upgrade_fork_schema)
        async with self.sessions() as session:
            account = XYAccount(owner_id=self.owner, account_id=self.account, unb=str(self.owner), cookie='fixture', login_method='cookie', status='active')
            card = Card(user_id=self.owner, name='fixture inventory', type='data', data_content='A\nB\nC')
            session.add_all([account, card]); await session.flush(); self.card_id = card.id
            session.add_all([XYOrder(owner_id=self.owner, account_id=self.account, order_no=self.account+'-1', item_id='fixture', status='pending_ship', quantity=2), XYOrder(owner_id=self.owner, account_id=self.account, order_no=self.account+'-2', item_id='fixture', status='pending_ship', quantity=2)])
            await session.commit()
        self.delivery = DeliveryExecution(self.sessions)
        self.replies = ReplyState(self.sessions)

    async def asyncTearDown(self):
        async with self.sessions() as session:
            for model in (DeliveryIntent, XYOrder, XYAccount):
                await session.execute(delete(model).where(model.owner_id == self.owner))
            await session.execute(delete(Card).where(Card.user_id == self.owner))
            for table in REPLY_TABLES:
                predicate = table.c.account_id == self.account if "account_id" in table.c else table.c.owner_id == self.owner
                await session.execute(delete(table).where(predicate))
            await session.commit()
        lease = AccountLease(self.redis, self.account)
        await self.redis.delete(lease.key, lease.generation_key)
        await self.redis.aclose(); await self.engine.dispose()

    async def test_competing_redis_holders_and_stale_generation(self):
        first, second = AccountLease(self.redis, self.account), AccountLease(self.redis, self.account)
        claims = await asyncio.gather(first.acquire(), second.acquire())
        self.assertEqual(sum(claims), 1)
        holder = first if claims[0] else second
        await self.redis.delete(holder.key)  # this fixture's crashed holder only
        successor = AccountLease(self.redis, self.account)
        self.assertTrue(await successor.acquire())
        self.assertGreater(successor.generation, holder.generation)
        self.assertFalse(await holder.release())
        await successor.check()
        with self.assertRaises(ExecutionLost): await holder.check()

    async def test_mysql_competing_orders_do_not_oversell(self):
        results = await asyncio.gather(*(self.delivery.reserve(self.owner, self.account, self.account+suffix, self.card_id) for suffix in ('-1','-2')), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, DeliveryIntent) for r in results), 1)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        async with self.sessions() as s:
            self.assertEqual((await s.get(Card, self.card_id)).data_content, 'C')

    async def test_mysql_repeated_payment_shares_one_intent(self):
        results = await asyncio.gather(*(self.delivery.reserve(self.owner, self.account, self.account+'-1', self.card_id) for _ in range(4)))
        self.assertEqual(len({r.id for r in results}), 1)
        async with self.sessions() as s:
            self.assertEqual((await s.get(Card, self.card_id)).data_content, 'C')

    async def test_mysql_once_slot_race_failure_and_unknown(self):
        results = await asyncio.gather(*(self.replies.reserve_once(self.account, 'chat', 'scope', f'req-{i}') for i in range(4)))
        self.assertEqual(sum(results), 1)
        request = f'req-{results.index(True)}'
        await self.replies.settle_once(self.account, 'chat', 'scope', request, 'unknown')
        self.assertFalse(await self.replies.reserve_once(self.account, 'chat', 'scope', 'new'))
        await self.replies.settle_once(self.account, 'chat', 'scope', request, 'failed')
        self.assertTrue(await self.replies.reserve_once(self.account, 'chat', 'scope', 'new'))
