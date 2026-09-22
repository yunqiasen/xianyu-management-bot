"""S3: synthetic SQLite only; never imports the legacy application's singleton DB."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

try:
    from tools.migration.snapshot import Snapshot, MigrationError
except ImportError:
    Snapshot = None
    MigrationError = Exception


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'synthetic.sqlite'
        with sqlite3.connect(self.path) as db:
            db.executescript('CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, email TEXT, password_hash TEXT);'
                             'CREATE TABLE cookies(id TEXT PRIMARY KEY, user_id INTEGER, value TEXT, pause_duration INTEGER);')
            db.execute('INSERT INTO users VALUES(1,?,?,?)', ('fixture', 'fixture@example.invalid', 'FAKE_PASSWORD_HASH'))
            db.execute('INSERT INTO cookies VALUES(?,?,?,?)', ('fixture-a', 1, 'FAKE_COOKIE_SECRET', 0))

    def snapshot(self):
        self.assertIsNotNone(Snapshot, 'S3 consistent snapshot implementation missing')
        return Snapshot.read(self.path)

    def test_consistent_inventory_and_zero_preserved_source_unchanged(self):
        before = self.path.read_bytes()
        snap = self.snapshot()
        self.assertEqual(snap.tables['cookies'][0]['pause_duration'], 0)
        self.assertEqual(snap.inventory()['cookies']['count'], 1)
        self.assertIn('value', snap.inventory()['cookies']['columns'])
        self.assertEqual(before, self.path.read_bytes())
        self.assertNotIn('FAKE_COOKIE_SECRET', json.dumps(snap.inventory()))

    def test_snapshot_detached_from_later_source_write(self):
        snap = self.snapshot()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE cookies SET pause_duration=20")
        self.assertEqual(snap.tables['cookies'][0]['pause_duration'], 0)

    def test_missing_table_fails_without_partial_plan(self):
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE users')
        self.assertIsNotNone(Snapshot, 'snapshot implementation missing')
        with self.assertRaisesRegex(MigrationError, 'missing_required_table'):
            Snapshot.read(self.path)

    def test_corrupt_input_error_contains_no_source_content(self):
        self.assertIsNotNone(Snapshot, 'snapshot implementation missing')
        self.path.write_bytes(b'FAKE_COOKIE_SECRET invalid sqlite')
        with self.assertRaises(MigrationError) as ctx:
            Snapshot.read(self.path)
        self.assertNotIn('FAKE_COOKIE_SECRET', str(ctx.exception))

    def test_duplicate_natural_identity_fails(self):
        with sqlite3.connect(self.path) as db:
            db.executescript("CREATE TABLE keywords(cookie_id TEXT, keyword TEXT, item_id TEXT, reply TEXT);"
                             "INSERT INTO keywords VALUES('fixture-a','hi',NULL,'one');"
                             "INSERT INTO keywords VALUES('fixture-a','hi','','two');")
        self.assertIsNotNone(Snapshot, 'snapshot implementation missing')
        with self.assertRaisesRegex(MigrationError, 'duplicate_source_identity'):
            Snapshot.read(self.path)

    def test_missing_file_does_not_create_empty_database(self):
        self.assertIsNotNone(Snapshot, 'snapshot implementation missing')
        missing = self.path.parent / 'absent.sqlite'
        with self.assertRaisesRegex(MigrationError, 'source_not_file'):
            Snapshot.read(missing)
        self.assertFalse(missing.exists())
