import importlib.util
import unittest
from stdlib_loader import ROOT

@unittest.skipUnless(importlib.util.find_spec('sqlalchemy'), 'waiting for shared SQLAlchemy environment')
class MigrationTests(unittest.TestCase):
    def test_additive_upgrade_twice_preserves_existing_rows_and_flags(self):
        from sqlalchemy import create_engine, text, inspect
        from common.models.product_reliability_migration import upgrade_products
        engine=create_engine('sqlite://')
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE xy_publish_logs (id INTEGER PRIMARY KEY, status VARCHAR(20))'))
            connection.execute(text("INSERT INTO xy_publish_logs VALUES (1, 'success')"))
            connection.execute(text('CREATE TABLE protected_account_fixture (id INTEGER, auto_polish BOOLEAN, auto_red_flower BOOLEAN)'))
            connection.execute(text('INSERT INTO protected_account_fixture VALUES (1, 0, 0)'))
            upgrade_products(connection); upgrade_products(connection)
            self.assertTrue({'xy_product_publish_batches','xy_product_operation_evidence','xy_product_feedback_attempts','xy_product_rate_templates','xy_product_polish_runs'} <= set(inspect(connection).get_table_names()))
            self.assertIn('publish_snapshot',{r['name'] for r in inspect(connection).get_columns('xy_publish_logs')})
            self.assertEqual(connection.execute(text('SELECT status FROM xy_publish_logs')).scalar(),'success')
            self.assertEqual(tuple(connection.execute(text('SELECT * FROM protected_account_fixture')).first()),(1,0,0))
