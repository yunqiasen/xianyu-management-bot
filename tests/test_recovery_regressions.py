"""Regression coverage for the live 2026-10-06 login hang; no platform calls."""
import queue
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from DrissionPage._units.listener import Listener
from utils.refresh_util import DrissionHandler
from utils.slider_orchestrator import run_slider_with_fallback, run_slider_async_with_fallback
from utils.xianyu_slider_stealth import XianyuSliderStealth


class RecoveryStopTests(unittest.IsolatedAsyncioTestCase):
    def slider(self, feedback):
        slider = XianyuSliderStealth.__new__(XianyuSliderStealth)
        slider.user_id = 'fixture'
        slider.risk_trigger_scene = 'token_refresh'
        slider.last_verification_feedback = feedback
        slider.run = Mock(return_value=(False, None))
        slider.async_run = AsyncMock(return_value=(False, None))
        return slider

    async def test_sync_and_async_stop_when_primary_requires_a_pause(self):
        for feedback in (
            {'status': 'preflight_deferred', 'message': '等待冷却'},
            {'status': 'hard_block', 'message': '需要人工验证'},
            {'status': 'failure', 'fail_code': 'rswfh6', 'message': '验证失败，点击框体重试'},
        ):
            for mode in ('sync', 'async'):
                with self.subTest(feedback=feedback, mode=mode):
                    factory = Mock()
                    slider = self.slider(feedback)
                    kwargs = dict(fallback_enabled=True, remote_enabled=False, handler_factory=factory)
                    if mode == 'sync':
                        result = run_slider_with_fallback(slider, 'https://example.test', **kwargs)
                    else:
                        result = await run_slider_async_with_fallback(slider, 'https://example.test', **kwargs)
                    self.assertFalse(result.success)
                    factory.assert_not_called()
                    self.assertNotEqual(result.message, '滑块验证失败')

    async def test_ordinary_failure_still_uses_configured_fallback(self):
        for mode in ('sync', 'async'):
            with self.subTest(mode=mode):
                handler = SimpleNamespace(get_cookies=Mock(return_value=None))
                factory = Mock(return_value=handler)
                slider = self.slider({'status': 'failure'})
                kwargs = dict(remote_enabled=False, fallback_enabled=True, handler_factory=factory)
                if mode == 'sync':
                    result = run_slider_with_fallback(slider, 'https://example.test', **kwargs)
                else:
                    result = await run_slider_async_with_fallback(slider, 'https://example.test', **kwargs)
                factory.assert_called_once()
                self.assertFalse(result.success)


class ListenerDeadlineTests(unittest.TestCase):
    def test_no_network_packets_returns_and_closes_browser(self):
        listener = Listener.__new__(Listener)
        listener.listening = True
        listener._driver = SimpleNamespace(is_running=True)
        listener._caught = queue.Queue()
        observed = []

        def steps(*, count, timeout=None):
            # Fail promptly on the buggy version instead of hanging the test suite.
            observed.append(timeout)
            if timeout is None or timeout <= 0 or timeout > 3:
                return iter(())
            return Listener.steps(listener, count=count, timeout=timeout)

        handler = DrissionHandler.__new__(DrissionHandler)
        handler.max_retries = 1
        handler.show_mouse_trace = False
        handler.slide_attempt = 0
        handler.page = SimpleNamespace(
            get=Mock(), listen=SimpleNamespace(start=Mock(), stop=Mock(), steps=steps))
        handler._slide = Mock()
        handler._detect_captcha = Mock(return_value=False)
        handler.get_cookies_string = Mock(return_value='unb=fixture')
        handler.close = Mock()
        # Patch module sleep only; Listener uses its own clock/sleep.
        with patch('utils.refresh_util.time', SimpleNamespace(time=time.time, sleep=Mock())), \
             patch('utils.refresh_util.log_captcha_event'):
            start = time.monotonic()
            result = handler.get_cookies('https://example.test', cookie_id='fixture')
            elapsed = time.monotonic() - start
        self.assertEqual(result, 'unb=fixture')
        self.assertEqual(len(observed), 1)
        self.assertIsNotNone(observed[0], 'Network listener has no deadline; this froze production for 8h')
        self.assertGreater(observed[0], 0)
        self.assertLessEqual(observed[0], 3)
        self.assertLess(elapsed, 4)
        handler.close.assert_called()
        handler.page.listen.stop.assert_called()


if __name__ == '__main__':
    unittest.main()
