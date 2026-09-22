import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from fixtures import KEY, make_source

class CLITests(unittest.TestCase):
    def test_inspect_emits_only_redacted_report_and_blocked_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source(Path(tmp) / 'synthetic.sqlite')
            run = subprocess.run([sys.executable, '-m', 'tools.migration', 'inspect', '--source', str(source),
                '--namespace', 'fixture', '--key-env', 'FIXTURE_MIGRATION_KEY'],
                env={**os.environ, 'FIXTURE_MIGRATION_KEY': KEY.hex()}, text=True, capture_output=True)
        self.assertEqual(run.returncode, 2, run.stderr)
        report = json.loads(run.stdout)
        self.assertTrue(report['blocked'])
        self.assertNotIn('FAKE_', run.stdout + run.stderr)

    def test_bad_source_never_prints_exception_trace_or_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'bad.sqlite'
            source.write_bytes(b'FAKE_COOKIE_BROKEN')
            run = subprocess.run([sys.executable, '-m', 'tools.migration', 'inspect', '--source', str(source),
                '--namespace', 'fixture', '--key-env', 'FIXTURE_MIGRATION_KEY'],
                env={**os.environ, 'FIXTURE_MIGRATION_KEY': KEY.hex()}, text=True, capture_output=True)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(json.loads(run.stdout)['error'], 'invalid_sqlite_snapshot')
        self.assertNotIn('FAKE_', run.stdout + run.stderr)

    def test_cli_explicit_legacy_key_wrong_key_redacted_before_target(self):
        import sqlite3
        from cryptography.fernet import Fernet
        with tempfile.TemporaryDirectory() as tmp:
            source=make_source(Path(tmp)/'source.sqlite'); key=Fernet.generate_key()
            with sqlite3.connect(source) as c:
                c.execute("UPDATE cookies SET value=? WHERE id='account-a'",('enc$'+Fernet(key).encrypt(b'FAKE_PRIVATE').decode(),))
            run=subprocess.run([sys.executable,'-m','tools.migration','inspect','--source',str(source),'--namespace','fixture',
                                '--source-key-env','FIXTURE_SOURCE_KEY'],env={**os.environ,'XYMB_MIGRATION_KEY_HEX':KEY.hex(),
                                'FIXTURE_SOURCE_KEY':Fernet.generate_key().decode()},capture_output=True,text=True)
            self.assertEqual(run.returncode,1)
            self.assertEqual(json.loads(run.stdout)['error'],'legacy_secret_authentication_failed')
            self.assertNotIn('FAKE_PRIVATE',run.stdout+run.stderr)

    def test_legacy_stop_cli_dryrun_requires_no_target_or_token(self):
        run=subprocess.run([sys.executable,'-m','tools.migration','legacy-stop','--legacy-url','http://127.0.0.1:9',
                            '--account','fixture','--dryrun'],capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertTrue(json.loads(run.stdout)['dryrun'])
