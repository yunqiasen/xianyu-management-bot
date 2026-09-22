"""S1/S2: fork metadata and isolated startup configuration."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

class RuntimeBaselineTests(unittest.TestCase):
    def test_fork_version_never_offers_upstream_binary(self):
        spec = importlib.util.spec_from_file_location('fork_version_service', ROOT / 'backend-web/app/services/version_service.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import asyncio
        with patch.object(module.httpx, 'AsyncClient', side_effect=AssertionError('must not contact upstream updater')):
            result = asyncio.run(module.check_update())
        self.assertFalse(result['has_update'])
        self.assertEqual(result['download_url'], '')
        self.assertIn('xianyu-management-bot', result['current_version'])
        self.assertEqual(result['repository'], 'https://github.com/yunqiasen/xianyu-management-bot')

    def test_scheduler_is_opt_in(self):
        spec = importlib.util.spec_from_file_location('scheduler_config_test', ROOT / 'scheduler/app/core/config.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertFalse(module.SchedulerConfig(_env_file=None).auto_start_scheduler)

    def test_isolated_compose_has_no_production_names_or_ports(self):
        import yaml
        cfg = yaml.safe_load((ROOT / 'compose.integration.yml').read_text())
        self.assertEqual(cfg['name'], 'xymb-integration')
        self.assertEqual(set(cfg['services']), {'mysql', 'redis', 'mysql-restore'})
        for service in cfg['services'].values():
            self.assertNotIn('container_name', service)
            self.assertTrue(all(str(port).startswith('127.0.0.1:') for port in service['ports']))
            self.assertFalse(any(str(port).startswith('127.0.0.1:9000:') for port in service['ports']))

if __name__ == '__main__':
    unittest.main()

class RestoreIsolationTests(unittest.TestCase):
    def test_restore_uses_separate_instance_and_opt_in_profile(self):
        import yaml
        cfg=yaml.safe_load((ROOT/'compose.integration.yml').read_text())
        restore=cfg['services']['mysql-restore']
        self.assertEqual(restore['profiles'], ['restore'])
        self.assertEqual(restore['ports'], ['127.0.0.1:19007:3306'])
        self.assertNotEqual(restore['volumes'],cfg['services']['mysql']['volumes'])
