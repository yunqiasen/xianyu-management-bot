"""显式隔离MySQL：阻塞前一事务，另连接补拉，重建进程状态后恢复。"""
import asyncio
import os
import unittest
from pathlib import Path
from uuid import uuid4
from sqlalchemy import insert, delete, select, MetaData
from unittest.mock import patch
import common.services.reply_state as state_module
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from common.models.reply_state import TABLES, reply_events
from common.services.reply_state import ReplyState

class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = os.environ.get('XYMB_INTEGRATION_ENV')
        if not path:
            raise RuntimeError('XYMB_INTEGRATION_ENV required')
        env = dict(line.split('=', 1) for line in Path(path).read_text().splitlines() if '=' in line and not line.startswith('#'))
        assert env['MYSQL_HOST'] == '127.0.0.1' and env['MYSQL_PORT'] == '19006' and env['MYSQL_DATABASE'] == 'xymb_integration'
        self.engine = create_async_engine(URL.create('mysql+asyncmy', username=env['MYSQL_USER'], password=env['MYSQL_PASSWORD'], host=env['MYSQL_HOST'], port=19006, database=env['MYSQL_DATABASE']))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = ReplyState(self.sessions)
        self.account = 'replies-stream-' + uuid4().hex
        suffix = uuid4().hex[:10]
        metadata = MetaData()
        self.events = reply_events.to_metadata(metadata, name='fixture_reply_events_' + suffix)
        self.heads = state_module.reply_stream_heads.to_metadata(metadata, name='fixture_reply_heads_' + suffix)
        self.patches = [patch.object(state_module, 'reply_events', self.events), patch.object(state_module, 'reply_stream_heads', self.heads)]
        for p in self.patches: p.start()
        self.metadata = metadata
        async with self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def asyncTearDown(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(self.metadata.drop_all) # 仅本测试UUID命名表
        for p in self.patches: p.stop()
        await self.engine.dispose()

    async def test_commit_fence_replay_across_connection_and_restart(self):
        # 第一个事务持有账号栅栏，在取得自增ID后故意延迟commit。
        async with self.sessions() as first:
            await self.store._stream_lock(first, self.account)
            await first.execute(insert(self.events).values(account_id=self.account, chat_id='c', event_id='first',
                message_id='first', role='user', origin='platform', sender_id='buyer', content='first', occurred_at=1))
            later = asyncio.create_task(self.store.record_message(self.account, 'c', 'second', 'user', 'second'))
            await asyncio.sleep(.15)
            self.assertFalse(later.done(), '后事务已越过前事务提交栅栏')
            self.assertEqual(await self.store.events(self.account), [], '未提交事件被提前发布')
            await first.commit()
            await later
        restarted = ReplyState(self.sessions)
        page1 = await restarted.events(self.account, limit=1)
        page2 = await restarted.events(self.account, after=page1[-1]['cursor'], limit=1)
        self.assertEqual([r['event_id'] for r in page1 + page2], ['first', 'second'])
        await restarted.update_message_status(self.account, 'c', 'first', 'unknown')
        self.assertEqual(await restarted.events(self.account, after=page2[-1]['cursor']), []) # confirmed终态不回退

    async def test_rollback_does_not_stall_watermark(self):
        async with self.sessions() as first:
            await self.store._stream_lock(first, self.account)
            await first.execute(insert(self.events).values(account_id=self.account, chat_id='c', event_id='rollback',
                message_id='rollback', role='user', origin='platform', sender_id='buyer', content='rollback', occurred_at=1))
            later = asyncio.create_task(self.store.record_message(self.account, 'c', 'kept', 'user', 'kept'))
            await asyncio.sleep(.1)
            await first.rollback()
            await later
        self.assertEqual([r['event_id'] for r in await self.store.events(self.account)], ['kept'])

    async def test_upgrade_legacy_message_id_is_backfilled(self):
        from sqlalchemy import text
        import common.models.reply_state as models
        self.assertTrue(hasattr(models, 'upgrade_reply_schema'), '旧库缺message_id未有增量迁移')
        async with self.engine.begin() as conn:
            await conn.execute(text('ALTER TABLE ' + self.events.name + ' DROP COLUMN message_id'))
            await conn.execute(text('INSERT INTO ' + self.events.name + " (account_id,chat_id,event_id,version,role,origin,sender_id,sender_name,avatar,item_id,content,content_type,status,occurred_at) VALUES (:account,'c','old',1,'user','platform','buyer','','','','old','text','confirmed',1)"), {'account':self.account})
            await conn.run_sync(lambda c: models.upgrade_reply_schema(c, events_table=self.events))
            await conn.run_sync(lambda c: models.upgrade_reply_schema(c, events_table=self.events))
        row = (await self.store.history(self.account, 'c'))[0]
        self.assertEqual(row['message_id'], 'old')

if __name__ == '__main__': unittest.main()
