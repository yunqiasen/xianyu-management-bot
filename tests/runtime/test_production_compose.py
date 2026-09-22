"""The rendered production topology keeps browser and service ports aligned."""
import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ProductionComposeTests(unittest.TestCase):
    def render(self, **overrides):
        env = dict(os.environ, XYMB_BUILD_COMMIT='a' * 40, XYMB_RELEASE_ID='a' * 12,
                   XYMB_RUNTIME_DIR='/tmp/xymb-compose-test', MYSQL_ROOT_PASSWORD='fixture-root',
                   MYSQL_PASSWORD='fixture-db', REDIS_PASSWORD='fixture-redis',
                   XYMB_RESTORE_ROOT_PASSWORD='fixture-restore-root',
                   XYMB_RESTORE_PASSWORD='fixture-restore', INTERNAL_API_TOKEN='fixture-' + 'a' * 40,
                   **overrides)
        result = subprocess.run(['docker', 'compose', '-f', str(ROOT / 'compose.production.yml'),
                                 'config', '--format', 'json'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_single_browser_origin_and_internal_service_ports(self):
        services = self.render()['services']
        self.assertEqual(str(services['frontend']['ports'][0]['published']), '9000')
        for name, port in [('backend-web', 8089), ('websocket', 8090), ('scheduler', 8091)]:
            row = services[name]
            self.assertEqual(row['environment']['XYMB_BUILD_COMMIT'], 'a' * 40)
            self.assertEqual(row['build']['args']['XYMB_BUILD_COMMIT'], 'a' * 40)
            self.assertEqual(row['ports'][0]['target'], port)
            self.assertEqual(row['ports'][0]['host_ip'], '127.0.0.1')
            self.assertIn(f':{port}/health', ' '.join(row['healthcheck']['test']))
        self.assertEqual(services['backend-web']['environment']['WEBSOCKET_SERVICE_URL'], 'http://websocket:8090')
        self.assertEqual(services['backend-web']['environment']['SCHEDULER_SERVICE_URL'], 'http://scheduler:8091')
        self.assertEqual(services['websocket']['environment']['BACKEND_WEB_SERVICE_URL'], 'http://backend-web:8089')
        self.assertEqual(services['scheduler']['environment']['WEBSOCKET_SERVICE_URL'], 'http://websocket:8090')
        for row in services.values():
            for mount in row.get('volumes', []):
                self.assertNotIn('xianyu-auto-reply-fix', mount.get('source', ''))
        self.assertEqual(services['backend-web']['environment']['AUTO_START_CRAWL_JOBS'], 'false')
        self.assertEqual(services['websocket']['environment']['AUTO_START_WEBSOCKET'], 'false')
        self.assertEqual(services['scheduler']['environment']['AUTO_START_SCHEDULER'], 'false')

    def test_candidate_port_override_does_not_change_container_routes(self):
        services = self.render(FRONTEND_PORT='19010', FRONTEND_BIND='127.0.0.1')['services']
        self.assertEqual(str(services['frontend']['ports'][0]['published']), '19010')
        self.assertEqual(services['frontend']['ports'][0]['target'], 80)
        self.assertEqual(services['frontend']['ports'][0]['host_ip'], '127.0.0.1')
        self.assertEqual(services['backend-web']['environment']['WEBSOCKET_SERVICE_URL'], 'http://websocket:8090')


if __name__ == '__main__':
    unittest.main()
