"""Known legacy history is preserved without becoming live work or buyer input."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from fixtures import KEY, make_source
from tools.migration.mapping import plan
from tools.migration.snapshot import Snapshot

class HistoricalImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = make_source(Path(self.tmp.name) / 'source.sqlite')

    def build(self):
        return plan(Snapshot.read(self.path), namespace='history', key=KEY)

    def test_supported_sha256_password_survives_migration_with_user_disabled(self):
        password_hash = hashlib.sha256(b'known-original-password').hexdigest()
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE users SET password_hash=? WHERE id=1', (password_hash,))
        user = next(s for s in self.build().steps if s.target == 'xy_users' and s.values['username'] == 'seller-a')
        self.assertEqual(user.values['password_hash'], password_hash)
        self.assertEqual(user.values['status'], 'INACTIVE')
