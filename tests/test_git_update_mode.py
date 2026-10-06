import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
import reply_server


class GitUpdateModeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'XYMB_UPDATE_MODE': 'git'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.overrides = dict(reply_server.app.dependency_overrides)
        reply_server.app.dependency_overrides[reply_server.get_current_user] = lambda: {
            'user_id': 1, 'username': 'fixture-admin', 'is_admin': True,
        }
        self.addCleanup(self.restore_overrides)
        self.client = TestClient(reply_server.app)

    def restore_overrides(self):
        reply_server.app.dependency_overrides.clear()
        reply_server.app.dependency_overrides.update(self.overrides)

    def test_check_reports_git_management_without_constructing_file_updater(self):
        with patch('reply_server.get_updater', side_effect=RuntimeError('read-only source')) as updater:
            result = self.client.get('/api/update/check')
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['success'])
        self.assertEqual(result.json()['data']['update_mode'], 'git')
        self.assertFalse(result.json()['data']['has_update'])
        self.assertFalse(result.json()['data']['update_available'])
        updater.assert_not_called()

    def test_managed_source_never_enters_file_update_or_restart(self):
        for method, path in (
            ('post', 'apply'), ('post', 'cleanup-backups'), ('post', 'save-hashes'),
            ('post', 'restart'), ('get', 'progress'), ('get', 'local-hashes'),
            ('get', 'file-changes'), ('get', 'saved-hashes'),
        ):
            with self.subTest(path=path), \
                 patch('reply_server.get_updater', side_effect=RuntimeError('must not run')) as updater, \
                 patch('reply_server.asyncio.create_task') as schedule:
                response = getattr(self.client, method)('/api/update/' + path)
                self.assertEqual(response.status_code, 409)
                updater.assert_not_called()
                schedule.assert_not_called()

    def test_non_managed_install_keeps_upstream_check(self):
        updater = Mock(current_version='fixture-version', check_for_updates=AsyncMock(return_value=None))
        with patch.dict(os.environ, {'XYMB_UPDATE_MODE': 'files'}), \
             patch('reply_server.get_updater', return_value=updater):
            result = self.client.get('/api/update/check')
        self.assertEqual(result.status_code, 200)
        updater.check_for_updates.assert_awaited_once()
        self.assertEqual(result.json()['data']['current_version'], 'fixture-version')


if __name__ == '__main__':
    unittest.main()
