import unittest
from unittest.mock import AsyncMock

class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_dependencies_and_worker_switch_are_visible(self):
        from common.runtime_health import service_health
        result = await service_health('message', False, AsyncMock(return_value=True), AsyncMock(return_value=True))
        self.assertTrue(result['success'])
        self.assertEqual(result['data']['database'], 'connected')
        self.assertEqual(result['data']['redis'], 'connected')
        self.assertFalse(result['data']['workers_enabled'])
        self.assertIn('xianyu-management-bot', result['data']['version'])

    async def test_dependency_failure_is_not_healthy_or_secret_bearing(self):
        from common.runtime_health import service_health
        result = await service_health('message', False, AsyncMock(side_effect=RuntimeError('password=fixture-secret')), AsyncMock(return_value=True))
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 503)
        self.assertEqual(result['data']['status'], 'degraded')
        self.assertNotIn('fixture-secret', str(result))
