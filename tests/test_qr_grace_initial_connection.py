"""Fresh QR cookies must be usable for the first message connection immediately."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from XianyuAutoAsync import XianyuLive, InitAuthError


class QrGraceInitialConnectionTests(unittest.IsolatedAsyncioTestCase):
    def live(self, grace=True):
        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = 'qr-grace-fixture'
        live.current_token = None
        live.last_token_refresh_time = 0
        live.token_refresh_interval = 3600
        live.device_id = 'fixture-device'
        live._is_in_qr_login_grace_period = Mock(return_value=grace)
        live._should_defer_auth_recovery_for_qr_grace = Mock(return_value=grace)
        live.last_token_refresh_error_message = '扫码登录稳定期中'
        live.last_token_refresh_status = 'not_started'
        live.send_token_refresh_notification = AsyncMock()
        async def refresh(**kwargs):
            live.current_token = 'fixture-token'
            live.last_token_refresh_status = 'success'
        live.refresh_token = AsyncMock(side_effect=refresh)
        return live

    async def test_fresh_qr_connects_immediately_without_password_relogin(self):
        live = self.live()
        ws = SimpleNamespace(send=AsyncMock())
        with patch('XianyuAutoAsync.asyncio.sleep', new=AsyncMock()):
            await live.init(ws)
        live.refresh_token.assert_awaited_once_with(allow_password_login_recovery=False)
        packets = [json.loads(call.args[0]) for call in ws.send.await_args_list]
        self.assertEqual(packets[0]['lwp'], '/reg')
        self.assertEqual(packets[0]['headers']['token'], 'fixture-token')
        self.assertEqual(packets[1]['lwp'], '/r/SyncStatus/ackDiff')

    async def test_valid_token_during_grace_needs_no_refresh(self):
        live = self.live()
        live.current_token = 'existing-token'
        live.last_token_refresh_time = time.time()
        with patch('XianyuAutoAsync.asyncio.sleep', new=AsyncMock()):
            await live.init(SimpleNamespace(send=AsyncMock()))
        live.refresh_token.assert_not_awaited()

    async def test_failed_initial_token_reports_real_failure_not_grace_wait(self):
        live = self.live()
        live.last_token_refresh_status = 'platform_auth_failed'
        live.refresh_token = AsyncMock(return_value=None)
        ws = SimpleNamespace(send=AsyncMock())
        with self.assertRaisesRegex(InitAuthError, 'platform_auth_failed'):
            await live.init(ws)
        live.refresh_token.assert_awaited_once_with(allow_password_login_recovery=False)
        ws.send.assert_not_awaited()

    async def test_outside_grace_keeps_normal_recovery(self):
        live = self.live(grace=False)
        with patch('XianyuAutoAsync.asyncio.sleep', new=AsyncMock()):
            await live.init(SimpleNamespace(send=AsyncMock()))
        live.refresh_token.assert_awaited_once_with(allow_password_login_recovery=True)
