"""S1: worker login routes are internal, not a public credential endpoint."""
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from app.api.routes import password_login
from app.core.config import get_settings


class WorkerLoginBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_login_poll_and_cancel_require_service_token(self):
        app = FastAPI()
        app.include_router(password_login.router)
        with patch.object(get_settings(), 'internal_api_token', 'fixture-' * 8):
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://fixture') as client:
                for method, path in [('GET', '/password-login/check/missing'),
                                     ('DELETE', '/password-login/cancel/missing')]:
                    with self.subTest(method=method):
                        response = await client.request(method, path)
                        self.assertEqual(response.status_code, 401)
                        response = await client.request(method, path,
                            headers={'X-Internal-Token': 'fixture-' * 8})
                        self.assertEqual(response.status_code, 200)
