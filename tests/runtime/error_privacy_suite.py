"""S1: real backend exception handling keeps response and logs free of raw input."""
import sys
from pathlib import Path
import unittest
import httpx
from loguru import logger

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend-web'), str(ROOT)]
import _bootstrap


class ErrorPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_unhandled_failure_never_exposes_exception_payload(self):
        secret = 'fixture-opaque-value-57c9'
        route = '/fixture-unhandled-error'

        async def fail():
            raise RuntimeError(secret)

        _bootstrap.app.add_api_route(route, fail)
        captured = []
        sink = logger.add(lambda message: captured.append(str(message)), level='ERROR')
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(
                    app=_bootstrap.app, raise_app_exceptions=False), base_url='http://fixture') as client:
                response = await client.get(route)
            self.assertEqual(response.json()['code'], 500)
            self.assertFalse(response.json()['success'])
            self.assertNotIn(secret, response.text)
            self.assertNotIn(secret, ''.join(captured))
            self.assertIn('RuntimeError', ''.join(captured))
            self.assertTrue(response.headers.get('x-correlation-id'))
        finally:
            logger.remove(sink)
            _bootstrap.app.router.routes[:] = [r for r in _bootstrap.app.router.routes
                                               if getattr(r, 'path', '') != route]


if __name__ == '__main__':
    unittest.main()
