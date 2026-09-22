"""S1/S3: 真实SQL事务、临时SQLite；无平台连接。"""
import asyncio
import importlib
from pathlib import Path
import tempfile
import unittest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker


class ReplyStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(Path('common/services/reply_state.py').exists(), '缺少持久回复业务入口')
        self.mod = importlib.import_module('common.services.reply_state')
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + self.tmp.name + '/state.db')
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda sync: self.mod.metadata.create_all(sync, tables=self.mod.TABLES))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = self.mod.ReplyState(self.sessions)

    async def asyncTearDown(self):
        if hasattr(self, 'engine'):
            await self.engine.dispose()
            self.tmp.cleanup()

    async def test_history_dedup_replay_and_account_isolation(self):
        args = dict(account_id='a', chat_id='same', event_id='e1', role='user', content='hello', sender_id='buyer')
        first, fresh = await self.store.record_message(**args)
        second, duplicate = await self.store.record_message(**args)
        await self.store.record_message(**{**args, 'account_id': 'b', 'content': 'private'})
        self.assertTrue(fresh)
        self.assertFalse(duplicate)
        self.assertEqual(first['cursor'], second['cursor'])
        await self.store.record_message(**{**args, 'event_id': 'e2', 'role': 'assistant', 'content': '人工承诺', 'origin': 'manual'})
        history = await self.store.history('a', 'same', limit=20)
        self.assertEqual([m['content'] for m in history], ['hello', '人工承诺'])
        replay = await self.store.events('a', after=first['cursor'])
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]['version'], 1)

    async def test_once_concurrency_unknown_and_explicit_failure(self):
        results = await asyncio.gather(*[self.store.reserve_once('a', 'c', '', str(i)) for i in range(8)])
        self.assertEqual(sum(results), 1)
        winner = str(results.index(True))
        await self.store.settle_once('a', 'c', '', winner, 'unknown')
        self.assertFalse(await self.store.reserve_once('a', 'c', '', 'new'))
        await self.store.settle_once('a', 'c', '', 'stale', 'failed')
        self.assertFalse(await self.store.reserve_once('a', 'c', '', 'new'))
        await self.store.settle_once('a', 'c', '', winner, 'failed')
        self.assertTrue(await self.store.reserve_once('a', 'c', '', 'new'))
        await self.store.settle_once('a', 'c', '', 'new', 'confirmed')
        await self.store.settle_once('a', 'c', '', 'new', 'failed')
        self.assertFalse(await self.store.reserve_once('a', 'c', '', 'again'))

    async def test_manual_pause_zero_and_isolation(self):
        await self.store.pause('a', 'c', 10, now=100)
        self.assertEqual(await self.store.pause_remaining('a', 'c', now=101), 599)
        self.assertEqual(await self.store.pause_remaining('b', 'c', now=101), 0)
        await self.store.pause('a', 'c', 0, now=102)
        self.assertEqual(await self.store.pause_remaining('a', 'c', now=102), 0)

    async def test_outbound_idempotency_and_unknown_never_resends(self):
        sent = []
        async def transport():
            sent.append(1)
            raise TimeoutError('lost ack')
        for _ in range(2):
            result = await self.store.send('a', 'c', 'request-1', '人工消息', transport, pause_minutes=10)
            self.assertEqual(result['status'], 'unknown')
        self.assertEqual(len(sent), 1)
        self.assertGreater(await self.store.pause_remaining('a', 'c'), 0)
        self.assertEqual((await self.store.history('a', 'c'))[0]['content'], '人工消息')
        with self.assertRaises(ValueError):
            await self.store.send('a', 'c', 'request-1', '不同消息', transport)

    async def test_outbound_confirmation_and_failure(self):
        async def confirmed(): return {'status': 'confirmed', 'messageId': 'm1'}
        async def failed(): return {'status': 'failed', 'error': 'rejected'}
        self.assertEqual((await self.store.send('a', 'c', 'r1', 'ok', confirmed))['status'], 'confirmed')
        self.assertEqual((await self.store.send('a', 'c', 'r2', 'no', failed))['status'], 'failed')
        self.assertEqual([m['content'] for m in await self.store.history('a', 'c')], ['ok'])

    async def test_blacklist_scope_toggle_and_delivery_independent(self):
        from common.models.xy_personal_blacklist import XYPersonalBlacklist
        from common.models.xy_platform_blacklist import XYPlatformBlacklist
        from sqlalchemy import insert, update
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda sync: self.mod.metadata.create_all(sync, tables=[XYPersonalBlacklist.__table__, XYPlatformBlacklist.__table__]))
        async with self.sessions() as session:
            await session.execute(insert(XYPersonalBlacklist.__table__).values(id=1, owner_id=1, account_id='a', buyer_id='buyer', item_id='item', is_enabled=True))
            await session.commit()
        self.assertTrue(await self.store.reply_blocked(1, 'a', 'buyer', 'item'))
        self.assertFalse(await self.store.reply_blocked(2, 'a', 'buyer', 'item'))
        self.assertFalse(await self.store.reply_blocked(1, 'b', 'buyer', 'item'))
        self.assertFalse(await self.store.reply_blocked(1, 'a', 'buyer', 'other'))
        async with self.sessions() as session:
            await session.execute(update(XYPersonalBlacklist.__table__).where(XYPersonalBlacklist.id == 1).values(is_enabled=False))
            await session.commit()
        self.assertFalse(await self.store.reply_blocked(1, 'a', 'buyer', 'item'))

    async def test_receipt_revision_is_replayed_with_new_cursor(self):
        row, _ = await self.store.record_message('a', 'c', 'out:request', 'assistant', 'hi', status='submitted')
        await self.store.update_message_status('a', 'c', 'out:request', 'confirmed')
        replay = await self.store.events('a', row['cursor'])
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]['message_id'], 'out:request')
        self.assertEqual(replay[0]['status'], 'confirmed')
        history = await self.store.history('a', 'c')
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['status'], 'confirmed')
        await self.store.update_message_status('a', 'c', 'out:request', 'unknown')
        self.assertEqual((await self.store.history('a', 'c'))[0]['status'], 'confirmed')
