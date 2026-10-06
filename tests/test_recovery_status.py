import unittest
from XianyuAutoAsync import XianyuLive


class RecoveryStatusTests(unittest.TestCase):
    def test_reports_actual_cooldown_reason_instead_of_blame_password(self):
        for reason, expected in {
            'slider_failed': '滑块验证失败',
            'verification_required': '需要人工验证',
            'credentials': '账号或密码错误',
            'risk_control': '平台风控',
            'browser_crash': '登录浏览器异常',
        }.items():
            with self.subTest(reason=reason):
                live = XianyuLive.__new__(XianyuLive)
                live.cookie_id = 'fixture'
                live.last_token_refresh_status = None
                live._get_active_password_login_failure_backoff = lambda _now: {
                    'reason': reason, 'remaining_time': 123,
                }
                self.assertTrue(live._should_skip_token_refresh_for_login_backoff(1000))
                self.assertEqual(live.last_token_refresh_status, 'password_login_backoff_wait')
                self.assertIn(expected, live.last_token_refresh_error_message)
                self.assertIn('123', live.last_token_refresh_error_message)
                if reason != 'credentials':
                    self.assertNotIn('密码登录失败', live.last_token_refresh_error_message)

    def test_no_backoff_leaves_current_status_unchanged(self):
        live = XianyuLive.__new__(XianyuLive)
        live._get_active_password_login_failure_backoff = lambda _now: None
        live.last_token_refresh_status = 'success'
        self.assertFalse(live._should_skip_token_refresh_for_login_backoff(1000))
        self.assertEqual(live.last_token_refresh_status, 'success')
