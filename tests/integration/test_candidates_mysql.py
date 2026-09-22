"""S1/S3: P5 facts survive MySQL collation, competing workers and restarts."""
import asyncio
import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from common.db.base_class import Base
from common.db.fork_schema import upgrade_fork_schema
from common.models.listing_monitor_task import ListingMonitorTask
from common.services.bargaining_history import BargainingHistory
from common.services.reply_state import ReplyState
from tools.verification.run import commerce_fixture


class CandidateMySQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = commerce_fixture()
        self.url = self.fixture.__enter__()
        self.engine = create_async_engine(self.url, hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(upgrade_fork_schema)
            await connection.run_sync(lambda conn: Base.metadata.create_all(
                conn, tables=[ListingMonitorTask.__table__]))

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.fixture.__exit__(None, None, None)

    async def test_confirmed_bargaining_identity_and_replay_after_restart(self):
        replies = ReplyState(self.sessions)
        for event_id in ('EVENT', 'event', ' event ', 'event '):
            _, fresh = await replies.record_message('account', 'chat', event_id, 'user', '便宜点吧')
            self.assertTrue(fresh, event_id)
        await replies.record_message('account', 'chat', 'inquiry', 'user', '多少钱')
        await replies.record_message('account', 'chat', 'review', 'user', '评价不错')
        await replies.record_message('account', 'chat', 'manual', 'assistant', '便宜点吧', origin='manual')
        await replies.record_message('account', 'chat', 'pending', 'user', '便宜点吧', status='unknown')
        await replies.record_message('other', 'chat', 'EVENT', 'user', '便宜点吧')
        results = await asyncio.gather(*(
            BargainingHistory(self.sessions).reconcile_shared('account', 'chat@goofish') for _ in range(4)),
            return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        self.assertEqual([result['count'] for result in results], [4, 4, 4, 4])
        self.assertEqual(sum(result['new_events'] for result in results), 4)
        await self.engine.dispose()
        self.engine = create_async_engine(self.url, hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        ledger = BargainingHistory(self.sessions)
        replay = await ledger.reconcile_shared('account', 'chat')
        self.assertEqual((replay['count'], replay['new_events']), (4, 0))
        self.assertEqual((await ledger.reconcile_shared('other', 'chat'))['count'], 1)
        self.assertEqual((await ledger.reconcile_shared('account', 'CHAT'))['count'], 0)

    async def test_identity_upgrade_preserves_existing_facts_and_is_repeatable(self):
        from sqlalchemy import text
        async with self.engine.begin() as connection:
            for table, collation in [('xy_reply_events', 'utf8mb4_unicode_ci'),
                                     ('xy_bargaining_events', 'utf8mb4_bin')]:
                await connection.execute(text(f'ALTER TABLE {table} MODIFY event_id '
                                              f'VARCHAR(128) COLLATE {collation} NOT NULL'))
        replies = ReplyState(self.sessions)
        await replies.record_message('account', 'chat', 'OLD', 'user', '便宜点吧', occurred_at=123.125)
        self.assertEqual((await BargainingHistory(self.sessions).reconcile_shared('account', 'chat'))['count'], 1)
        async with self.engine.begin() as connection:
            await connection.run_sync(upgrade_fork_schema)
            await connection.run_sync(upgrade_fork_schema)
        before = await replies.history('account', 'chat')
        self.assertEqual([(row['event_id'], row['content'], row['occurred_at']) for row in before],
                         [('OLD', '便宜点吧', 123.125)])
        for event_id in ('old', 'old '):
            _, fresh = await replies.record_message('account', 'chat', event_id, 'user', '便宜点吧')
            self.assertTrue(fresh)
        ledger = await BargainingHistory(self.sessions).reconcile_shared('account', 'chat')
        self.assertEqual((ledger['count'], ledger['new_events']), (3, 2))

    async def test_monitor_competing_pages_restart_stale_generations_and_price_cycles(self):
        from common.services.listing_monitor_reliability import MonitorReliabilityService
        async with self.sessions() as session:
            session.add_all([ListingMonitorTask(id=task_id, owner_id=7, keyword='相机',
                account_ids=['account'], collect_pages=2, is_enabled=True, monitor_type=kind)
                for task_id, kind in [(1, 'listing'), (2, 'price_drop')]])
            await session.commit()
        monitor = MonitorReliabilityService(self.sessions)

        async def together(*calls):
            results = await asyncio.gather(*calls, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return results

        def response(context, rows=()):
            return {**context, 'http_status': 200, 'ret': ['SUCCESS::调用成功'], 'items': list(rows)}

        def item(item_id, price='100'):
            return {'item_id': item_id, 'price': price, 'title': '相机', 'area': '杭州'}

        generations = await together(*(monitor.begin(1, 'account') for _ in range(4)))
        self.assertEqual(generations, [1, 1, 1, 1])
        p1, p2 = [await monitor.page_context(1, page) for page in (1, 2)]
        self.assertEqual((await monitor.accept(p2, response(p2, [item('two')]))).status, 'partial')
        self.assertFalse((await monitor.status(1))['baseline_ready'])
        await self.engine.dispose()
        self.engine = create_async_engine(self.url, hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        monitor = MonitorReliabilityService(self.sessions)
        self.assertEqual(await monitor.page_context(1, 2), p2)
        results = await together(*(monitor.accept(p1, response(p1, [item('one'), item('two')]))
                                  for _ in range(4)))
        self.assertCountEqual([result.status for result in results],
                              ['baseline_complete', 'duplicate_response', 'duplicate_response', 'duplicate_response'])
        state = await monitor.status(1)
        self.assertEqual([row['item_id'] for row in state['items']], ['one', 'two'])
        self.assertEqual(state['events'], [])
        generations = await together(*(monitor.begin(1, 'account') for _ in range(4)))
        self.assertEqual(generations, [2, 2, 2, 2])
        self.assertEqual((await monitor.accept(p1, response(p1, [item('late')]))).status, 'stale_response')
        q1, q2 = [await monitor.page_context(1, page) for page in (1, 2)]
        await together(monitor.accept(q2, response(q2, [item('new', '90')])),
                       monitor.accept(q1, response(q1, [item('new', '100'), item('one')])))
        state = await monitor.status(1)
        self.assertEqual([(row['item_id'], row['kind'], row['price']) for row in state['events']],
                         [('new', 'listing', '100')])
        for price in ('100', '80', '100', '80'):
            await monitor.begin(2, 'account')
            r1, r2 = [await monitor.page_context(2, page) for page in (1, 2)]
            await together(monitor.accept(r1, response(r1, [item('same', price)])),
                           monitor.accept(r2, response(r2)))
        events = (await monitor.status(2))['events']
        self.assertEqual([(row['kind'], row['old_price'], row['price']) for row in events],
                         [('price_drop', '100', '80'), ('price_drop', '100', '80')])
        self.assertNotEqual(events[0]['id'], events[1]['id'])
        claims = await together(monitor.lease(1, 'first', 100), monitor.lease(1, 'second', 100))
        self.assertEqual(sum(claims), 1)
        self.assertTrue(await monitor.lease(1, 'successor', 221))
        self.assertFalse(await monitor.renew(1, 'first', 222))
        await monitor.release(1, 'first')
        self.assertFalse(await monitor.lease(1, 'third', 222))
