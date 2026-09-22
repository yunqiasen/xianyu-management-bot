"""S1: actual scheduler bootstrap retains internal RPC HTTP errors and redacts input."""
from pathlib import Path
import importlib.util
import sys
import unittest
from httpx import AsyncClient, ASGITransport

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scheduler'))
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('scheduler_bootstrap_fixture', ROOT / 'scheduler/_bootstrap.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SchedulerConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_configuration_uses_real_status_and_never_echoes_input(self):
        from app.core.config import get_settings
        from unittest.mock import patch
        with patch.object(get_settings(), 'internal_api_token', 'fixture-' * 8):
            async with AsyncClient(transport=ASGITransport(app=module.app), base_url='http://fixture') as client:
                denied = await client.post('/internal/account-configuration', json={})
                self.assertEqual(denied.status_code, 401)
                invalid = await client.post('/internal/account-configuration',
                    headers={'X-Internal-Token':'fixture-' * 8},
                    json={'owner_id':'SECRET_INPUT', 'config_version':0, 'account_id':'fixture'})
                self.assertEqual(invalid.status_code, 422)
                self.assertNotIn('SECRET_INPUT', invalid.text)


if __name__ == '__main__':
    unittest.main()
