"""S1/S2：真实 ORM、临时 SQLite、真实运行服务，Redis 为命令边界替身。"""
import asyncio
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).parents[2] / 'backend-web'))
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import BigInteger, event
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.mysql import LONGTEXT

@compiles(LONGTEXT, "sqlite")
def sqlite_longtext(*args, **kwargs):
    return "TEXT"

@compiles(BigInteger, "sqlite")
def sqlite_bigint(*args, **kwargs):
    return "INTEGER"
from common.models.xy_account import XYAccount
from common.services import account_policy as policy
from test_execution import RedisBoundary


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database_dir = tempfile.TemporaryDirectory(prefix='xymb-accounts-')
        self.addCleanup(self.database_dir.cleanup)
        self.engine = create_async_engine('sqlite+aiosqlite:///' + str(Path(self.database_dir.name) / 'accounts.db'))
        @event.listens_for(self.engine.sync_engine, 'connect')
        def disable_legacy_transactions(dbapi, record):
            dbapi.isolation_level = None
            # Request and BackgroundTasks use separate sessions. WAL preserves the
            # reader snapshot while the worker commits, as MySQL does in production.
            cursor = dbapi.cursor()
            cursor.execute('PRAGMA journal_mode=WAL')
            cursor.close()
        @event.listens_for(self.engine.sync_engine, 'begin')
        def explicit_begin(connection):
            connection.exec_driver_sql('BEGIN')
        async with self.engine.begin() as conn:
            await conn.run_sync(XYAccount.__table__.create)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.factory() as db:
            db.add(XYAccount(id=1, owner_id=7, account_id='fixture', cookie='unb=101; token=old',
                             unb='101', login_method='manual', status='active',
                             username='seller', login_password='secret'))
            await db.commit()

    async def create_owner(self):
        from common.models.user import User
        async with self.engine.begin() as connection:
            await connection.run_sync(User.__table__.create, checkfirst=True)
        async with self.factory() as session:
            session.add(User(id=7, username='owner', email='owner@example.test', password_hash='fixture-hash'))
            await session.commit()

    async def create_deletion_tables(self):
        from common.models.xy_order import XYOrder
        from common.models.delivery_intent import DeliveryIntent
        from common.models.account_operation import AccountOperation
        from common.models.reply_state import reply_outbox
        async with self.engine.begin() as connection:
            for table in (XYOrder.__table__, DeliveryIntent.__table__, AccountOperation.__table__, reply_outbox):
                await connection.run_sync(table.create, checkfirst=True)

    async def asyncTearDown(self):
        await self.engine.dispose()


class RuntimeTests(DatabaseCase):
    async def test_runtime_service_exists(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('common.services.account_runtime'))

    async def test_competition_manual_disable_and_config_fencing(self):
        from common.services.account_runtime import AccountRuntime
        from common.services.account_execution import ExecutionLost
        redis = RedisBoundary()
        first = AccountRuntime('fixture', 7, redis, self.factory)
        second = AccountRuntime('fixture', 7, redis, self.factory)
        self.assertTrue(await first.start())
        self.assertFalse(await second.start())
        await first.check()
        async with self.factory() as db:
            a=await db.get(XYAccount,1); a.status='disabled'; await db.commit()
        with self.assertRaises(ExecutionLost): await first.check()
        await first.close()

    async def test_credential_job_cancel_and_late_result_real_database(self):
        from app.services.account_service import AccountService
        async with self.factory() as db:
            svc=AccountService(db); a=await db.get(XYAccount,1)
            job = await svc.start_credential_job(a, 'cookie_import', 7)
            await svc.cancel_credential_job(a, job['id'], 7)
            self.assertFalse(await svc.finish_credential_job(a, job['id'], 'unb=101; token=new'))
        async with self.factory() as db:
            a=await db.get(XYAccount,1)
            self.assertEqual(a.cookie, 'unb=101; token=old')
            self.assertEqual(a.login_password,'secret')

    async def test_config_ack_is_partial_and_never_accepts_old_version(self):
        from app.services.account_service import AccountService
        async with self.factory() as db:
            svc=AccountService(db); a=await db.get(XYAccount,1)
            await svc.update_status(a, False)
            self.assertFalse(await svc.ack_configuration(a,'scheduler',0))
            self.assertTrue(await svc.ack_configuration(a,'scheduler',1))
            self.assertEqual(policy.pending_consumers(a),['websocket'])

    async def test_delete_preview_blocks_unfinished_orders(self):
        from common.models.xy_order import XYOrder
        from app.services.account_service import AccountService
        await self.create_deletion_tables()
        async with self.factory() as db:
            db.add(XYOrder(id=1,owner_id=7,account_id='fixture',order_no='fixture-order',status='pending'))
            await db.commit()
            a=await db.get(XYAccount,1); svc=AccountService(db)
            preview=await svc.delete_preview(a)
            self.assertFalse(preview['can_delete'])
            with self.assertRaises(ValueError): await svc.delete_account(a)
            self.assertIsNotNone(await db.get(XYAccount,1))

    async def test_configuration_change_blocks_old_worker(self):
        from common.services.account_runtime import AccountRuntime
        from common.services.account_execution import ExecutionLost
        runtime=AccountRuntime('fixture',7,RedisBoundary(),self.factory)
        await runtime.start()
        async with self.factory() as db:
            a=await db.get(XYAccount,1); policy.bump_config(a); await db.commit()
        with self.assertRaises(ExecutionLost): await runtime.check()
        await runtime.close()

    async def test_proxy_failure_persists_and_blocks_business(self):
        from common.services.account_runtime import AccountRuntime
        from common.services.account_execution import ExecutionLost
        runtime=AccountRuntime('fixture',7,RedisBoundary(),self.factory)
        await runtime.start()
        await runtime.pause('proxy')
        async with self.factory() as db:
            self.assertEqual(policy.snapshot(await db.get(XYAccount,1))['business_state'],'proxy_error')
        with self.assertRaises(ExecutionLost): await runtime.check()
        await runtime.close()

class RegistrationTests(DatabaseCase):
    async def test_new_connection_drops_old_ready_state_until_registration_receipt(self):
        from common.services.account_runtime import AccountRuntime
        async with self.factory() as session:
            account = await session.get(XYAccount, 1)
            state = policy.snapshot(account)
            state.update(business_state='ready', connection_state='connected')
            policy.store(account, state)
            await session.commit()
        runtime = AccountRuntime('fixture', 7, RedisBoundary(), self.factory)
        self.assertTrue(await runtime.start())
        try:
            async with self.factory() as session:
                state = policy.snapshot(await session.get(XYAccount, 1))
                self.assertEqual(state['business_state'], 'unchecked')
                self.assertEqual(state['connection_state'], 'connecting')
        finally:
            await runtime.close()

    async def test_only_matching_positive_registration_ack_marks_business_ready(self):
        from common.services.account_runtime import AccountRuntime
        runtime = AccountRuntime('fixture', 7, RedisBoundary(), self.factory)
        self.assertTrue(await runtime.start())
        try:
            await runtime.record_connection(True)
            for message in ({'code':200,'headers':{'mid':'old'},'body':{}},
                            {'headers':{'mid':'registration'},'body':{}},
                            {'code':200,'headers':{'mid':'registration'},'body':{'reason':'rejected'}}):
                self.assertFalse(await runtime.confirm_registration(message, 'registration'))
            async with self.factory() as session:
                self.assertNotEqual(policy.snapshot(await session.get(XYAccount, 1))['business_state'], 'ready')
            self.assertTrue(await runtime.confirm_registration({'code':200,'headers':{'mid':'registration'},'body':{}}, 'registration'))
            async with self.factory() as session:
                state = policy.snapshot(await session.get(XYAccount,1))
                self.assertEqual(state['business_state'], 'ready')
                self.assertIsNotNone(state['last_success_at'])
        finally:
            await runtime.close()

    async def test_startup_binding_uses_current_cookie_and_proxy_then_fences_updates(self):
        from common.services.account_runtime import AccountRuntime
        from common.services.account_execution import ExecutionLost
        async with self.factory() as session:
            account = await session.get(XYAccount, 1)
            policy.replace_credentials(account, 'unb=101; token=new', expected_version=0)
            account.proxy_type, account.proxy_host, account.proxy_port = 'http', '127.0.0.1', 8008
            await session.commit()
        runtime = AccountRuntime('fixture', 7, RedisBoundary(), self.factory)
        await runtime.start()
        try:
            binding = await runtime.load_binding()
            self.assertEqual(binding['cookie'], 'unb=101; token=new')
            self.assertEqual(binding['proxy']['proxy_port'], 8008)
            self.assertEqual(binding['owner_id'], 7)
            async with self.factory() as session:
                account = await session.get(XYAccount, 1)
                policy.replace_credentials(account, 'unb=101; token=newer', expected_version=1)
                await session.commit()
            with self.assertRaises(ExecutionLost):
                await runtime.load_binding()
        finally:
            await runtime.close()
