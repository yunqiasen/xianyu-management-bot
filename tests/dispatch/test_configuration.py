"""S2: applying configuration never creates a competing account executor."""
import asyncio
import unittest
import test_flow as fixtures
from common.services.account_configuration import ConfigurationApplyRequest, ConfigurationStore
from common.services import account_policy


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FlowTests.asyncSetUp
    asyncTearDown = fixtures.FlowTests.asyncTearDown

    async def test_offline_consumer_applies_configuration_without_starting_an_account(self):
        from app.services.xianyu.cookie_manager import CookieManager
        manager = CookieManager(asyncio.get_running_loop())
        result = await manager.apply_configuration(ConfigurationApplyRequest(
            account_id='fixture', owner_id=7, config_version=0), self.sessions)
        self.assertTrue(result['applied'])
        self.assertEqual(result['status'], 'applied_offline')
        self.assertEqual(manager.tasks, {})
        account = await ConfigurationStore(self.sessions).read(7, 'fixture', 0)
        self.assertEqual(account_policy.snapshot(account)['consumers']['websocket'], 0)

    async def test_stuck_previous_executor_is_not_replaced_or_acknowledged(self):
        from app.services.xianyu.cookie_manager import CookieManager
        manager = CookieManager(asyncio.get_running_loop())
        started, release = asyncio.Event(), asyncio.Event()
        async def pending_external():
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        task = asyncio.create_task(pending_external())
        manager.tasks['fixture'] = task
        manager.instances['fixture'] = self.live
        await started.wait()
        try:
            result = await manager.apply_configuration(ConfigurationApplyRequest(
                account_id='fixture', owner_id=7, config_version=0), self.sessions, stop_timeout=.02)
            self.assertFalse(result['applied'])
            self.assertEqual(result['status'], 'previous_executor_pending')
            self.assertIs(manager.tasks['fixture'], task)
            self.assertFalse(task.done())
        finally:
            release.set()
            await task
