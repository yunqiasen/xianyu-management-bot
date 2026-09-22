import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from redis.asyncio import Redis
from common.services.account_execution import AccountLease, ExecutionLost
from tools.migration.snapshot import MigrationError
from fixtures import KEY
try:
    from tools.migration.handoff import Handoff
except ImportError:
    Handoff = None


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = Redis.from_url('redis://127.0.0.1:19379/15')
        try:
            await self.redis.ping()
        except Exception:
            await self.redis.aclose()
            self.skipTest('isolated Redis at 19379 not available')
        self.account = 'fixture-migration-' + uuid.uuid4().hex
        self.old = AccountLease(self.redis, self.account)
        self.new = AccountLease(self.redis, self.account)
        await self.old.acquire()
        self.tmp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        if hasattr(self, 'old'):
            await self.redis.delete(self.old.key, self.old.generation_key)
            self.tmp.cleanup()
            await self.redis.aclose()

    def tool(self):
        self.assertIsNotNone(Handoff, 'single-account rehearsal coordinator missing')
        return Handoff(self.account, self.old, self.new, key=KEY, archive_dir=Path(self.tmp.name))

    async def test_unresolved_inflight_keeps_new_executor_stopped(self):
        tool = self.tool()
        events = []
        async def stop():
            events.append('stop')
            return {'stopped': True, 'watermark': 7, 'inflight': [{'kind': 'send', 'state': 'unknown'}]}
        async def final_import():
            self.fail('unresolved send must block final import')
        report = await tool.switch(stop_old=stop, final_import=final_import)
        self.assertEqual(report['state'], 'paused_inflight')
        self.assertFalse(self.new.generation)
        with self.assertRaises(ExecutionLost):
            await self.old.check()

    async def test_stop_snapshot_import_then_acquire_and_old_generation_is_fenced(self):
        tool = self.tool()
        events = []
        async def stop():
            events.append('stop')
            return {'stopped': True, 'watermark': 7, 'inflight': []}
        async def final_import():
            with self.assertRaises(ExecutionLost):
                await self.old.check()
            events.append('final_snapshot_and_import')
            return {'verified': True, 'blocked': False, 'checksum': '1' * 64, 'checkpoint': 15, 'account_disabled': True}
        report = await tool.switch(stop_old=stop, final_import=final_import)
        self.assertEqual(report['state'], 'lease_acquired_account_disabled')
        self.assertEqual(events, ['stop', 'final_snapshot_and_import'])
        self.assertGreater(self.new.generation, self.old.generation)
        await self.new.check()
        with self.assertRaises(ExecutionLost):
            await self.old.check()
        self.assertEqual(tool.read_state()['checkpoint'], 15)

    async def test_import_error_releases_both_and_persists_sanitized_failure(self):
        tool = self.tool()
        async def stop():
            return {'stopped': True, 'watermark': 8, 'inflight': []}
        async def final_import():
            raise RuntimeError('FAKE_COOKIE_DO_NOT_LOG')
        report = await tool.switch(stop_old=stop, final_import=final_import)
        self.assertEqual(report['state'], 'paused_import_error')
        self.assertNotIn('FAKE_', str(report))
        self.assertIsNone(await self.redis.get(self.old.key))

    async def test_failed_stop_does_not_claim_success_or_acquire_new(self):
        tool = self.tool()
        async def stop():
            return {'stopped': False, 'watermark': None, 'inflight': []}
        report = await tool.switch(stop_old=stop, final_import=None)
        self.assertEqual(report['state'], 'old_stop_unconfirmed')
        await self.old.check()
        self.assertEqual(self.new.generation, 0)

    async def test_restart_and_rollback_never_reactivate_old_without_reconciliation(self):
        tool = self.tool()
        async def stop():
            return {'stopped': True, 'watermark': 8, 'inflight': []}
        async def final_import():
            return {'verified': True, 'blocked': False, 'checksum': 'a'*64, 'checkpoint': 1, 'account_disabled': True}
        await tool.switch(stop_old=stop, final_import=final_import)
        async def export():
            self.assertIsNone(await self.redis.get(self.old.key))
            return {'bundle': 'rollback-fixture.enc', 'checksum': 'b'*64, 'state': 'paused_reconciliation_required'}
        report = await tool.rollback(stop_new=stop, export_delta=export)
        self.assertEqual(report['state'], 'paused_reconciliation_required')
        self.assertIsNone(await self.redis.get(self.old.key))
        restarted = self.tool()
        self.assertEqual(restarted.read_state()['state'], 'paused_reconciliation_required')

    async def test_concurrent_coordinators_do_not_overwrite_checkpoint(self):
        one, two = self.tool(), self.tool()
        entered, proceed = asyncio.Event(), asyncio.Event()
        async def stop():
            entered.set()
            await proceed.wait()
            return {'stopped': True, 'watermark': 9, 'inflight': []}
        async def final_import():
            return {'verified': True, 'blocked': False, 'checksum': 'c'*64, 'checkpoint': 1, 'account_disabled': True}
        first = asyncio.create_task(one.switch(stop_old=stop, final_import=final_import))
        await entered.wait()
        try:
            with self.assertRaisesRegex(MigrationError, 'handoff_coordinator_busy'):
                await asyncio.wait_for(two.switch(stop_old=stop, final_import=final_import), timeout=.2)
        finally:
            proceed.set()
            await first
        self.assertEqual(one.read_state()['state'], 'lease_acquired_account_disabled')
