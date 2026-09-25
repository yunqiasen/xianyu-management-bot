"""Image health probes must match the IPv4-only nginx listener."""
from pathlib import Path
import unittest

class FrontendHealthTests(unittest.TestCase):
    def test_health_probe_uses_configured_loopback_family(self):
        root = Path(__file__).resolve().parents[2]
        dockerfile = (root / 'docker/frontend/Dockerfile').read_text()
        command = dockerfile.split('HEALTHCHECK', 1)[1]
        self.assertIn('http://127.0.0.1/', command)
        self.assertNotIn('http://localhost', command)
