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

    def test_known_platform_notices_are_system_history_not_buyer_requests(self):
        with sqlite3.connect(self.path) as db:
            db.execute('ALTER TABLE chat_messages ADD COLUMN content_type INTEGER DEFAULT 1')
            db.execute('UPDATE chat_messages SET content_type=25')
        prepared = self.build()
        events = [s.values for s in prepared.steps if s.source == 'chat_messages']
        self.assertTrue(events)
        self.assertTrue(all(r['role'] == 'system' and r['content_type'] == 'system' for r in events))
        self.assertFalse(any(i['code'] == 'unsupported_message_type' for i in prepared.issues))

    def test_unknown_message_types_still_block(self):
        with sqlite3.connect(self.path) as db:
            db.execute('ALTER TABLE chat_messages ADD COLUMN content_type INTEGER DEFAULT 999')
        self.assertTrue(any(i['code'] == 'unsupported_message_type' and i['blocking'] for i in self.build().issues))
