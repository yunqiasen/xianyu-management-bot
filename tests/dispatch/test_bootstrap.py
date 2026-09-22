"""S1: inspect the application actually served by websocket/main.py."""
import importlib.util
from pathlib import Path
import unittest
from httpx import AsyncClient, ASGITransport

ROOT = Path(__file__).resolve().parents[2]


class WorkerBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_app_mounts_operation_routes(self):
        spec = importlib.util.spec_from_file_location('ws_bootstrap_fixture', ROOT/'websocket/_bootstrap.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        async with AsyncClient(transport=ASGITransport(app=module.app), base_url='http://fixture') as client:
            for path in ['/internal/account-operations', '/internal/account-operations/renew']:
                with self.subTest(path=path):
                    response = await client.post(path, json={})
                    self.assertEqual(response.status_code, 401)
