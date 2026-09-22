"""S3 verification CLI: collect the actual multi-service entry points."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / 'tools/verification/run.py'


class VerificationPlanTests(unittest.TestCase):
    def test_plan_includes_nonstandard_suites_and_correct_service_namespaces(self):
        process = subprocess.run([sys.executable, str(RUNNER), '--plan'], cwd=ROOT,
                                 text=True, capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        suites = {suite['name']: suite for suite in json.loads(process.stdout)}
        self.assertEqual(suites['commerce-ws_scheduler']['pythonpath'], 'websocket:.')
        self.assertIn('replies-mysql_stream_suite', suites)
        self.assertIn('runtime-auth_boundary_suite', suites)
        self.assertIn('dispatch-mysql', suites)
        self.assertIn('upstream', suites)
        self.assertIn('verification', suites)
        self.assertIn('frontend-build', suites)
        self.assertIn('products-browser', suites)
        self.assertIn('ai-browser', suites)
        self.assertIn('bargaining-browser', suites)
        self.assertIn('admin-browser', suites)


class VerificationExecutionTests(unittest.TestCase):
    def test_suite_artifacts_stay_with_evidence_outside_the_source(self):
        from tools.verification.run import Suite, run_suite
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            evidence = Path(temp) / 'evidence'
            root.mkdir()
            evidence.mkdir()
            script = ("import os; from pathlib import Path; "
                      "p=Path(os.environ['XYMB_VERIFY_OUTPUT']); "
                      "(p/'proof.txt').write_text('fixture')")
            result = run_suite(Suite('artifacts', (sys.executable, '-c', script), kind='browser'),
                               root, evidence / 'artifacts.log', {}, 10)
            self.assertTrue(result['passed'], (evidence / 'artifacts.log').read_text())
            self.assertEqual((evidence / 'artifacts-artifacts/proof.txt').read_text(), 'fixture')
            self.assertEqual(list(root.iterdir()), [])

    def test_timeout_stops_the_suite_and_is_not_success(self):
        from tools.verification.run import Suite, run_suite
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suite = Suite('timeout', (sys.executable, '-c', 'import time; time.sleep(20)'), kind='browser')
            result = run_suite(suite, root, root / 'timeout.log', {}, .05)
            self.assertFalse(result['passed'])
            self.assertTrue(result['timed_out'])
            self.assertEqual(result['exit'], 124)

    def test_integration_guard_rejects_other_database_before_running(self):
        from tools.verification.run import validate_integration_environment
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'private.env'
            path.write_text('MYSQL_HOST=127.0.0.1\nMYSQL_PORT=19006\nMYSQL_DATABASE=production\n')
            with self.assertRaisesRegex(ValueError, 'isolated_environment_required'):
                validate_integration_environment(path)

    def test_skipped_and_empty_runs_are_not_passing_evidence(self):
        from tools.verification.run import Suite, run_suite
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'test_fixture.py').write_text("import unittest\n@unittest.skip('fixture unavailable')\nclass T(unittest.TestCase):\n def test_one(self): pass\n")
            command = (sys.executable, '-m', 'unittest', 'discover', '-s', str(root), '-v')
            skipped = run_suite(Suite('skipped', command), root, root / 'skipped.log', {}, 10)
            self.assertFalse(skipped['passed'])
            self.assertEqual(skipped['skipped'], 1)
            (root / 'test_fixture.py').unlink()
            empty = run_suite(Suite('empty', command), root, root / 'empty.log', {}, 10)
            self.assertFalse(empty['passed'])
            self.assertEqual(empty['tests'], 0)


if __name__ == '__main__':
    unittest.main()
