"""窗口、语义与持久事件的独立回归；不连接现用库。"""
import asyncio
import tempfile
import unittest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker


def row(event, content, role='user', **extra):
    return dict(account_id='a', chat_id='c', event_id=event, role=role, content=content,
                status='confirmed', origin='platform', content_type='text', **extra)


class IntentTests(unittest.TestCase):
    def test_candidate_flag_defaults_closed(self):
        import os
        from unittest.mock import patch
        from common.core.config import BaseConfig
        with patch.dict(os.environ, {}, clear=True):
            self.assertIs(BaseConfig(_env_file=None).xymb_enable_bargaining_v2, False)

    def test_conservative_bargaining_classification(self):
        from common.services.bargaining_history import classify_intent
        negatives = ['评价', '评价不错', '性价比如何', '刀', '价', '剃须刀', '这个还能用吗',
                     '剃须刀怎么用', '刀具参数', '优惠券怎么用', '如何设置底价提醒',
                     '100元是原价吗', '这把刀多少钱', '价格是多少', '能包邮吗',
                     '不砍价，直接拍', '不用优惠了', '不用便宜了', '请不要降价', '砍价功能怎么设置']
        positives = ['便宜点吧', '能便宜20元吗', '可以优惠吗', '最低多少', '底价多少',
                     '能少点吗', '再降10块吧', '80元卖吗', '80可以吗', '100包邮收', '能刀吗', '小刀一下', '包个邮吧']
        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(classify_intent(text).bargaining)
        for text in positives:
            with self.subTest(text=text):
                self.assertTrue(classify_intent(text).bargaining)
        self.assertEqual(classify_intent('剃须刀怎么用').intent, 'tech')
        self.assertEqual(classify_intent('多少钱').intent, 'price')
        self.assertEqual(classify_intent('剃须刀多少钱').intent, 'price')
        self.assertEqual(classify_intent('评价').intent, 'default')

    def test_window_keeps_groups_and_reports_filtered_and_oversize(self):
        from common.services.bargaining_history import select_history
        rows = [row('1', '旧问'), row('2', '旧答', 'assistant'), row('3', '新问'),
                row('4', '人工承诺明日发货', 'assistant'), row('5', '最新提问')]
        selected, diag = select_history(rows, budget=45)
        self.assertEqual([r['event_id'] for r in selected], ['3', '4', '5'])
        self.assertEqual(diag['trimmed_messages'], 2)
        bad = {**row('6', '待确认'), 'status': 'unknown'}
        selected, diag = select_history(rows + [bad], budget=300)
        self.assertEqual(diag['filtered_messages'], 1)
        self.assertNotIn('6', [r['event_id'] for r in selected])
        with self.assertRaises(ValueError):
            select_history([row('big', '长' * 500)], budget=20)


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from common.models.bargaining_event import BargainingEvent
        self.tmp = tempfile.TemporaryDirectory()
        self.url = f'sqlite+aiosqlite:///{self.tmp.name}/ledger.db'
        self.engine = create_async_engine(self.url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(BargainingEvent.__table__.create)

    async def asyncTearDown(self):
        if hasattr(self, 'engine'):
            await self.engine.dispose()
            self.tmp.cleanup()

    async def test_concurrent_duplicate_scope_and_fresh_engine(self):
        from common.services.bargaining_history import BargainingHistory
        history = [row('platform-event', '便宜点吧')]
        results = await asyncio.gather(*[BargainingHistory(self.sessions).record_confirmed('a', 'c@goofish', history) for _ in range(5)])
        self.assertTrue(all(result['count'] == 1 for result in results))
        service = BargainingHistory(self.sessions)
        other = [{**history[0], 'account_id': 'b'}]
        self.assertEqual((await service.record_confirmed('b', 'c', other))['count'], 1)
        await self.engine.dispose()
        fresh = create_async_engine(self.url)
        try:
            service = BargainingHistory(async_sessionmaker(fresh, expire_on_commit=False))
            self.assertEqual((await service.record_confirmed('a', 'c', []))['count'], 1)
            self.assertEqual((await service.record_confirmed('a', 'd', []))['count'], 0)
            with self.assertRaises(ValueError):
                await service.record_confirmed('a', 'c', other)
        finally:
            await fresh.dispose()

    async def test_event_identity_preserves_case_and_bytes(self):
        from common.services.bargaining_history import BargainingHistory
        rows = [row('EVENT', '便宜点吧'), row('event', '便宜点吧'), row(' event ', '便宜点吧')]
        result = await BargainingHistory(self.sessions).record_confirmed('a', 'c', rows)
        self.assertEqual(result['count'], 3)

    async def test_requires_platform_confirmed_buyer_identity(self):
        from common.services.bargaining_history import BargainingHistory
        rows = [row('valid', '能便宜点吗'),
                {**row('pending', '能便宜点吗'), 'status': 'unknown'},
                {**row('manual', '能便宜点吗'), 'origin': 'manual'},
                row('seller', '能便宜点吗', 'assistant'),
                {**row('', '能便宜点吗')},
                {**row('image', '便宜点'), 'content_type': 'image'},
                row('inquiry', '多少钱')]
        result = await BargainingHistory(self.sessions).record_confirmed('a', 'c', rows)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['missing_event_ids'], 1)
