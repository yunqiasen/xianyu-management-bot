import unittest
from types import SimpleNamespace
import importlib.util
from pathlib import Path
_spec = importlib.util.spec_from_file_location("account_policy", Path(__file__).parents[2] / "common/services/account_policy.py")
p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p)


def account():
    return SimpleNamespace(id=1, account_id='a', owner_id=7, unb='101', cookie='unb=101; token=old',
                           status='disabled', disable_reason='manual', username='seller',
                           login_password='secret', show_browser=False, proxy_type='none', metadata_json={'reply': {'enabled': False}})


class PolicyTests(unittest.TestCase):
    def test_blank_and_masked_secrets_preserved_explicit_clear_only(self):
        a = account()
        for value in (None, '', '  ', '******', '[REDACTED_SECRET]', '••••••'):
            p.update_login_fields(a, username=value, login_password=value)
            self.assertEqual((a.username, a.login_password), ('seller', 'secret'))
        p.update_login_fields(a, clear_fields=['login_password'])
        self.assertIsNone(a.login_password)
        self.assertEqual(a.username, 'seller')
        with self.assertRaises(ValueError):
            p.update_login_fields(a, clear_fields=['cookie'])

    def test_credentials_preserve_disabled_profile_and_reject_wrong_identity(self):
        a = account()
        p.replace_credentials(a, 'unb=101; token=new', expected_version=0)
        self.assertEqual(a.status, 'disabled')
        self.assertEqual(a.disable_reason, 'manual')
        self.assertEqual(a.metadata_json['reply'], {'enabled': False})
        self.assertEqual(a.login_password, 'secret')
        self.assertEqual(p.snapshot(a)['credential_version'], 1)
        with self.assertRaises(p.StaleAccountOperation):
            p.replace_credentials(a, 'unb=101; token=late', expected_version=0)
        with self.assertRaises(ValueError):
            p.replace_credentials(a, 'unb=other; token=new', expected_version=1)

    def test_config_partial_apply_old_ack_ignored_false_zero_and_none_preserved(self):
        a = account()
        p.bump_config(a, {'pause': 0, 'enabled': False, 'inherited': None})
        self.assertEqual(p.snapshot(a)['config_values'], {'pause': 0, 'enabled': False, 'inherited': None})
        self.assertEqual(p.pending_consumers(a), ['websocket', 'scheduler'])
        self.assertFalse(p.ack_config(a, 'websocket', 0))
        self.assertTrue(p.ack_config(a, 'websocket', 1))
        self.assertEqual(p.pending_consumers(a), ['scheduler'])

    def test_recovery_budget_cooldown_and_three_invalidations(self):
        a = account(); a.status = 'active'
        self.assertEqual(p.begin_recovery(a, 'network', now=100, jitter=0)['action'], 'reconnect')
        self.assertEqual(p.begin_recovery(a, 'network', now=101)['action'], 'coalesced')
        p.finish_recovery(a, success=False, now=110, jitter=0)
        self.assertEqual(p.begin_recovery(a, 'invalid_credentials', now=111)['action'], 'cooldown')
        self.assertEqual(p.begin_recovery(a, 'invalid_credentials', now=170)['action'], 'password_login')
        p.finish_recovery(a, success=False, now=180)
        self.assertEqual(p.begin_recovery(a, 'network', now=500)['action'], 'paused')
        for now in (1000, 1100, 1200):
            p.record_invalidation(a, now=now)
        self.assertEqual(p.snapshot(a)['business_state'], 'paused')
        self.assertEqual(p.snapshot(a)['reason'], 'repeated_invalid_credentials')

    def test_proxy_and_rate_limit_never_password_login(self):
        for reason, action in [('proxy', 'proxy_error'), ('rate_limit', 'cooldown'), ('verification', 'verification_required')]:
            a = account(); a.status = 'active'
            self.assertEqual(p.begin_recovery(a, reason, now=100)['action'], action)
            self.assertEqual(p.snapshot(a)['recovery_attempts'], 0)

    def test_verification_single_session_ownership_expiry_and_disabled_preserved(self):
        a = account()
        job = p.start_job(a, owner_id=7, kind='verification', now=100)
        self.assertEqual(job['expires_at'], 1000)
        self.assertEqual(p.start_job(a, owner_id=7, kind='verification', now=101)['id'], job['id'])
        with self.assertRaises(PermissionError):
            p.get_job(a, job['id'], owner_id=8, now=101)
        self.assertFalse(p.complete_job(a, job['id'], 'unb=101; token=new', now=1001))
        self.assertEqual(p.get_job(a, job['id'], owner_id=7, now=1001)['status'], 'expired')
        self.assertEqual(a.cookie, 'unb=101; token=old')
        self.assertEqual(a.status, 'disabled')

    def test_job_cancel_and_late_success_do_not_overwrite(self):
        a = account()
        job = p.start_job(a, owner_id=7, kind='cookie_import', now=100)
        p.cancel_job(a, job['id'], owner_id=7, now=101)
        self.assertFalse(p.complete_job(a, job['id'], 'unb=101; token=new', now=102))
        self.assertEqual(p.get_job(a, job['id'], owner_id=7, now=102)['status'], 'cancelled')
        self.assertEqual(a.cookie, 'unb=101; token=old')

    def test_fixed_proxy_validation_encoding_and_browser_capability(self):
        self.assertIsNone(p.proxy_url({'proxy_type': 'none'}))
        cfg = dict(proxy_type='http', proxy_host='::1', proxy_port=8123, proxy_user='u@x', proxy_pass='p:/#')
        self.assertEqual(p.proxy_url(cfg), 'http://u%40x:p%3A%2F%23@[::1]:8123')
        for port in (0, -1, 65536, True):
            with self.assertRaises(ValueError):
                p.proxy_url(dict(cfg, proxy_port=port))
        with self.assertRaises(ValueError):
            p.proxy_url(dict(cfg, proxy_host='host/path'))
        with self.assertRaises(ValueError):
            p.browser_proxy(dict(cfg, proxy_type='socks5'))

    def test_three_rate_limits_pause_without_password_login(self):
        a=account();a.status='active'
        self.assertEqual(p.begin_recovery(a,'rate_limit',now=100)['next_retry_at'],160)
        self.assertEqual(p.begin_recovery(a,'rate_limit',now=160)['next_retry_at'],460)
        self.assertEqual(p.begin_recovery(a,'rate_limit',now=460)['action'],'paused')
        self.assertEqual(p.snapshot(a)['recovery_attempts'],0)

    def test_late_failure_does_not_pause_new_credentials(self):
        a=account()
        job=p.start_job(a,owner_id=7,kind='cookie_import',now=100)
        p.replace_credentials(a,'unb=101; token=new',expected_version=0)
        self.assertFalse(p.fail_job(a,job['id'],now=101))
        self.assertEqual(p.snapshot(a)['business_state'],'unchecked')
        self.assertEqual(p.get_job(a,job['id'],owner_id=7,now=101)['status'],'superseded')

if __name__ == '__main__':
    unittest.main()
