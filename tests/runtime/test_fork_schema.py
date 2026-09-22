"""S3: additive setup is idempotent and leaves existing business facts intact."""
import unittest
from sqlalchemy import create_engine, inspect, text

class ForkSchemaTests(unittest.TestCase):
    def test_fresh_schema_and_second_upgrade(self):
        from common.db.fork_schema import upgrade_fork_schema
        engine = create_engine('sqlite://')
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE legacy_fact (value TEXT)'))
            conn.execute(text("INSERT INTO legacy_fact VALUES ('keep')"))
            upgrade_fork_schema(conn)
            upgrade_fork_schema(conn)
            tables = set(inspect(conn).get_table_names())
            self.assertTrue({'xy_ai_presets', 'xy_reply_events', 'xy_reply_policies', 'xy_product_polish_schedules'}.issubset(tables))
            self.assertEqual(conn.execute(text('SELECT value FROM legacy_fact')).scalar(), 'keep')

if __name__ == '__main__':
    unittest.main()

class UpgradeWiringTests(unittest.TestCase):
    def test_startup_upgrades_legacy_reply_events_and_preserves_message(self):
        from common.db.fork_schema import upgrade_fork_schema
        engine = create_engine('sqlite://')
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE xy_reply_events (event_id VARCHAR(128))'))
            conn.execute(text("INSERT INTO xy_reply_events VALUES ('legacy-message')"))
            upgrade_fork_schema(conn)
            upgrade_fork_schema(conn)
            self.assertIn('message_id', {c['name'] for c in inspect(conn).get_columns('xy_reply_events')})
            self.assertEqual(conn.execute(text('SELECT message_id FROM xy_reply_events')).scalar(), 'legacy-message')

    def test_candidate_tables_exist_without_turning_on_candidate_features(self):
        from common.db.fork_schema import upgrade_fork_schema
        engine=create_engine('sqlite://')
        with engine.begin() as conn:
            upgrade_fork_schema(conn)
            self.assertTrue({'xy_bargaining_events','xy_listing_monitor_state','xy_listing_monitor_pages',
                             'xy_listing_monitor_observations','xy_listing_monitor_events'}.issubset(inspect(conn).get_table_names()))

class AccountIdentityMigrationTests(unittest.TestCase):
    def test_identity_unique_across_owners_and_duplicate_migration_preserves_rows(self):
        from common.db.fork_schema import upgrade_account_identity
        from sqlalchemy.exc import IntegrityError
        engine=create_engine('sqlite://')
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE xy_accounts (id INTEGER PRIMARY KEY, owner_id INTEGER, unb VARCHAR(64))'))
            connection.execute(text("INSERT INTO xy_accounts VALUES (1,7,'101'),(2,8,'101')"))
            with self.assertRaisesRegex(ValueError,'duplicate_platform_identity'):
                upgrade_account_identity(connection)
            self.assertEqual(connection.execute(text('SELECT COUNT(*) FROM xy_accounts')).scalar(),2)
            connection.execute(text("UPDATE xy_accounts SET unb='202' WHERE id=2"))
            upgrade_account_identity(connection); upgrade_account_identity(connection)
            with self.assertRaises(IntegrityError):
                connection.execute(text("INSERT INTO xy_accounts VALUES (3,9,'101')"))
            self.assertEqual(connection.execute(text('SELECT COUNT(*) FROM xy_accounts')).scalar(),2)
