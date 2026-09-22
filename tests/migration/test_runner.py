import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from sqlalchemy import create_engine, select, update, func
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from fixtures import KEY, make_source
from tools.migration.mapping import plan, model_tables
from tools.migration.snapshot import Snapshot, MigrationError
try:
    from tools.migration.runner import Migrator, migration_metadata
except ImportError:
    Migrator = None


@compiles(LONGTEXT, 'sqlite')
def compile_longtext(element, compiler, **kwargs):
    return 'TEXT'


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source = make_source(root / 'source.sqlite')
        self.target = root / 'target.sqlite'
        self.engine = create_engine('sqlite:///' + str(self.target))
        self.addCleanup(self.engine.dispose)
        self.key = KEY
        self.plan = plan(Snapshot.read(self.source), namespace='fixture', key=KEY)
        tables = model_tables()
        from common.models.reply_state import TABLES
        selected = {s.target for s in self.plan.steps} | {t.name for t in TABLES}
        for name in selected:
            tables[name].create(self.engine, checkfirst=True)
        self.archive = root / 'batch'

    def runner(self):
        self.assertIsNotNone(Migrator, 'transactional model importer missing')
        migration_metadata.create_all(self.engine)
        return Migrator(self.engine, key=KEY, archive_dir=self.archive)

    def rebuild(self):
        return plan(Snapshot.read(self.source), namespace='fixture', key=KEY)

    def count(self, table):
        with self.engine.connect() as c:
            return c.scalar(select(func.count()).select_from(model_tables()[table]))

    def test_default_blocks_incomplete_mapping_and_quarantine_imports_real_models(self):
        runner = self.runner()
        with self.assertRaisesRegex(MigrationError, 'mapping_blocked'):
            runner.apply(self.plan)
        self.assertEqual(self.count('xy_accounts'), 0)
        report = runner.apply(self.plan, quarantine=True)
        self.assertEqual(report['inserted'], len(self.plan.steps))
        self.assertEqual(self.count('xy_orders'), 1)
        with self.engine.connect() as c:
            accounts = c.execute(select(model_tables()['xy_accounts'])).mappings().all()
        self.assertTrue(all(r['status'] == 'disabled' for r in accounts))
        self.assertEqual(accounts[0]['pause_duration'], 0)

    def test_repeat_and_each_checkpoint_restart_without_duplicate(self):
        runner = self.runner()
        for n in range(len(self.plan.steps)):
            with self.assertRaisesRegex(MigrationError, 'injected_interrupt'):
                runner.apply(self.plan, quarantine=True, interrupt_after=n)
        runner.apply(self.plan, quarantine=True)
        r = runner.apply(self.rebuild(), quarantine=True)
        self.assertEqual(r['unchanged'], len(self.plan.steps))
        self.assertEqual(self.count('xy_ai_chat_messages'), 2)
        self.assertEqual(self.count('xy_reply_events'), 3)

    def test_source_change_requires_incremental_and_target_change_is_never_overwritten(self):
        runner = self.runner()
        runner.apply(self.plan, quarantine=True)
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE cookies SET remark='source changed' WHERE id='account-a'")
        with self.assertRaisesRegex(MigrationError, 'incremental_required'):
            runner.apply(self.rebuild(), quarantine=True)
        runner.apply(self.rebuild(), quarantine=True, incremental=True)
        accounts = model_tables()['xy_accounts']
        with self.engine.begin() as c:
            c.execute(update(accounts).where(accounts.c.account_id == 'account-a').values(remark='new target fact'))
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE cookies SET remark='stale source' WHERE id='account-a'")
        with self.assertRaisesRegex(MigrationError, 'target_changed'):
            runner.apply(self.rebuild(), quarantine=True, incremental=True)
        with self.engine.connect() as c:
            self.assertEqual(c.scalar(select(accounts.c.remark).where(accounts.c.account_id == 'account-a')), 'new target fact')

    def test_ai_settings_change_is_part_of_account_checkpoint(self):
        runner = self.runner()
        runner.apply(self.plan, quarantine=True)
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE ai_reply_settings SET max_bargain_rounds=4")
        runner.apply(self.rebuild(), quarantine=True, incremental=True)
        a = model_tables()['xy_accounts']
        with self.engine.connect() as c:
            meta = c.scalar(select(a.c.metadata).where(a.c.account_id == 'account-a'))
        self.assertEqual(meta['ai_reply_settings']['max_bargain_rounds'], 4)

    def test_target_natural_conflict_and_corrupt_journal_do_not_overwrite(self):
        runner = self.runner()
        user = model_tables()['xy_users']
        with self.engine.begin() as c:
            c.execute(user.insert().values(id=9, username='seller-a', email='existing@example.invalid', password_hash='FAKE_TARGET_SECRET'))
        with self.assertRaisesRegex(MigrationError, 'target_identity_conflict') as ctx:
            runner.apply(self.plan, quarantine=True)
        self.assertNotIn('FAKE_', str(ctx.exception))
        self.assertEqual(self.count('xy_accounts'), 0)

    def test_encrypted_archive_preserves_unknown_data_and_source_stays_unchanged(self):
        runner = self.runner()
        before = self.source.read_bytes()
        runner.apply(self.plan, quarantine=True)
        self.assertEqual(before, self.source.read_bytes())
        for f in self.archive.iterdir():
            self.assertNotIn(b'FAKE_', f.read_bytes())
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        snap = runner.read_archive(self.plan.checksum)
        self.assertEqual(snap['tables']['system_settings'][0]['value'], 'FAKE_SETTING_SECRET')
        self.assertEqual(snap['tables']['data_card_reservations'][0]['reserved_content'], 'FAKE_RESERVED_CARD')

    def test_imported_once_and_history_are_consumed_by_real_reply_service(self):
        self.runner().apply(self.plan, quarantine=True)
        async def check():
            from common.services.reply_state import ReplyState
            engine = create_async_engine('sqlite+aiosqlite:///' + str(self.target))
            try:
                service = ReplyState(async_sessionmaker(engine, expire_on_commit=False))
                self.assertFalse(await service.reserve_once('account-a', 'chat-shared', '', 'new-event'))
                self.assertTrue(await service.reserve_once('account-b', 'chat-shared', '', 'new-event'))
                self.assertEqual([r['content'] for r in await service.history('account-a', 'chat-shared')], ['hello', 'manual promise'])
                self.assertEqual([r['content'] for r in await service.history('account-b', 'chat-shared')], ['B only'])
            finally:
                await engine.dispose()
        asyncio.run(check())

    def test_rollback_export_keeps_new_message_order_and_reserved_card_facts(self):
        runner = self.runner()
        runner.apply(self.plan, quarantine=True)
        order = model_tables()['xy_orders']
        events = model_tables()['xy_reply_events']
        with self.engine.begin() as c:
            c.execute(order.insert().values(id=42, owner_id=self.plan.steps[0].values['id'], account_id='account-a',
                order_no='new-order', status='pending_verification', metadata={'reservation': 'FAKE_NEW_CARD'}))
            c.execute(events.insert().values(account_id='account-a', chat_id='new-chat', event_id='new-event', message_id='new-event',
                role='user', origin='platform', sender_id='new-buyer', content='new message', occurred_at=123))
        report = runner.export_rollback(self.plan, account_id='account-a')
        self.assertEqual(report['state'], 'paused_reconciliation_required')
        self.assertEqual(report['counts']['xy_orders'], 2)
        self.assertEqual(report['counts']['xy_reply_events'], 3)
        self.assertEqual(self.count('xy_orders'), 2)
        self.assertNotIn('FAKE_', json.dumps(report))
        payload = runner.read_bundle(report['bundle'])
        self.assertIn('FAKE_NEW_CARD', json.dumps(payload))

    def test_history_import_is_chronological_not_hashed_cursor_order(self):
        with sqlite3.connect(self.source) as db:
            for n in range(2, 12):
                db.execute('INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?)',
                    (n, 'account-a', 'chat-shared', 'seller-a', f'message-{n}', 1, 'manual', f'2026-09-01 10:{n:02d}:00'))
        self.runner().apply(self.rebuild(), quarantine=True)
        async def check():
            from common.services.reply_state import ReplyState
            engine = create_async_engine('sqlite+aiosqlite:///' + str(self.target))
            try:
                service = ReplyState(async_sessionmaker(engine))
                history = await service.history('account-a', 'chat-shared')
                self.assertEqual([r['content'] for r in history], ['hello', 'manual promise'] + [f'message-{n}' for n in range(2, 12)])
            finally:
                await engine.dispose()
        asyncio.run(check())

    def test_migrated_delivery_payment_identity_reuses_intent_with_no_platform_calls(self):
        self.runner().apply(self.plan, quarantine=True)
        async def check():
            from common.services.delivery_execution import DeliveryExecution
            engine = create_async_engine('sqlite+aiosqlite:///' + str(self.target))
            try:
                service = DeliveryExecution(async_sessionmaker(engine, expire_on_commit=False))
                owner = self.plan.steps[0].values['id']
                result = await service.reserve(owner, 'account-a', 'order-a', 123)
                self.assertEqual(result.content_state, 'unknown')
                async with engine.begin() as c:
                    await c.execute(update(model_tables()['xy_accounts']).where(model_tables()['xy_accounts'].c.account_id == 'account-a').values(status='active'))
                calls = []
                async def outbound(*a, **kw):
                    calls.append(a)
                    return 'confirmed'
                await service.execute(result.id, owner, send=outbound, confirm=outbound)
                self.assertEqual(calls, [])
            finally:
                await engine.dispose()
        asyncio.run(check())

    def test_verify_checks_real_target_and_marks_removed_source_rows_for_reconciliation(self):
        runner = self.runner()
        runner.apply(self.plan, quarantine=True)
        self.assertTrue(hasattr(runner, 'verify'), 'database verification missing')
        report = runner.verify(self.plan, account_id='account-a')
        self.assertTrue(report['verified'])
        self.assertTrue(report['account_disabled'])
        self.assertTrue(report['blocked'])
        with sqlite3.connect(self.source) as db:
            db.execute('DELETE FROM keywords')
        changed = self.rebuild()
        runner.apply(changed, quarantine=True, incremental=True)
        report = runner.verify(changed, account_id='account-a')
        self.assertEqual(report['removed_source_rows'], 1)
        self.assertEqual(self.count('xy_keyword_rules'), 1)

    def test_extended_reply_and_notification_settings_are_consumed_after_actual_import(self):
        from fixtures import add_extended_source
        add_extended_source(self.source)
        extended = self.rebuild()
        for name in {s.target for s in extended.steps} | {'xy_platform_blacklist'}:
            model_tables()[name].create(self.engine, checkfirst=True)
        self.runner().apply(extended, quarantine=True)
        async def check():
            from common.services.reply_state import ReplyState
            from common.services.reply_policy import evaluate_filters
            from common.services.notification_template_service import NotificationTemplateService
            engine = create_async_engine('sqlite+aiosqlite:///' + str(self.target))
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            owner = extended.steps[0].values['id']
            try:
                replies = ReplyState(sessions)
                self.assertEqual((await replies.policy('account-a'))['strategy'], 'legacy')
                self.assertEqual((await replies.exclusive('account-a', 'item-a'))['content'], 'exclusive')
                self.assertTrue(await replies.reply_blocked(owner, 'account-a', 'blocked-buyer'))
                self.assertFalse(await replies.reply_blocked(owner, 'account-b', 'blocked-buyer'))
                self.assertTrue(evaluate_filters(await replies.filters('account-a'), 'ignore', 'user', '').blocks_reply)
                async with sessions() as session:
                    rendered, failed = await NotificationTemplateService(session).render_event(owner, 'message', {'account_id': 'account-a', 'summary': 'fixture event'})
                    self.assertEqual(rendered, 'account-a: fixture event')
                    self.assertFalse(failed)
            finally:
                await engine.dispose()
        asyncio.run(check())

    def test_tampered_encrypted_archive_fails_before_target_write(self):
        runner = self.runner()
        runner.apply(self.plan, quarantine=True)
        name = self.archive / ('snapshot-' + self.plan.checksum + '.enc')
        name.write_bytes(b'corrupt')
        with self.assertRaisesRegex(MigrationError, 'archive_integrity_failed'):
            runner.apply(self.plan, quarantine=True)
        self.assertEqual(self.count('xy_orders'), 1)

    def test_checkpoint_tampering_and_key_change_fail_closed(self):
        from tools.migration.runner import checkpoints
        runner = self.runner()
        runner.apply(self.plan, quarantine=True)
        with self.engine.begin() as c:
            c.execute(update(checkpoints).values(target_hash='0'*64))
        with self.assertRaisesRegex(MigrationError, 'target_changed'):
            runner.apply(self.plan, quarantine=True)

    def test_local_attachments_are_encrypted_and_traversal_is_blocked(self):
        runner = self.runner()
        self.assertTrue(hasattr(runner, 'archive_attachments'), 'attachment archive missing')
        root = Path(self.tmp.name) / 'assets'
        root.mkdir()
        (root / 'fixture.png').write_bytes(b'FAKE_IMAGE_BINARY')
        report = runner.archive_attachments(['fixture.png'], root=root)
        self.assertEqual(report['verified_count'], 1)
        self.assertNotIn('FAKE_', (self.archive / report['bundle']).read_text())
        with self.assertRaisesRegex(MigrationError, 'attachment_path_escape'):
            runner.archive_attachments(['../source.sqlite'], root=root)
        with self.assertRaisesRegex(MigrationError, 'attachment_not_found'):
            runner.archive_attachments(['absent.png'], root=root)

    def test_duplicate_planned_primary_key_stops_before_any_write(self):
        runner = self.runner()
        users = [s for s in self.plan.steps if s.target == 'xy_users']
        users[1].values['id'] = users[0].values['id']
        with self.assertRaisesRegex(MigrationError, 'duplicate_target_primary_key'):
            runner.apply(self.plan, quarantine=True)
        self.assertEqual(self.count('xy_users'), 0)
