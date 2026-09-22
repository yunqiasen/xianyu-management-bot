import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from fixtures import KEY, make_source
from tools.migration.snapshot import Snapshot, MigrationError
try:
    from tools.migration.mapping import plan
except ImportError:
    plan = None


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = make_source(Path(self.tmp.name) / 'fixture.sqlite')

    def build(self):
        self.assertIsNotNone(plan, 'target-model mapping not implemented')
        return plan(Snapshot.read(self.path), namespace='fixture', key=KEY)

    def steps(self, table):
        return [s for s in self.build().steps if s.target == table]

    def test_account_preserves_credentials_proxy_zero_and_legacy_policy_disabled(self):
        a = self.steps('xy_accounts')[0].values
        self.assertEqual(a['cookie'], 'FAKE_COOKIE_A')
        self.assertEqual(a['login_password'], 'FAKE_LOGIN_PASSWORD')
        self.assertEqual(a['pause_duration'], 0)
        self.assertEqual(a['proxy_port'], 0)
        self.assertEqual(a['status'], 'disabled')
        self.assertEqual(a['remark'], 'keep remark')
        self.assertEqual(a['metadata']['reply_policy'], 'legacy')
        self.assertEqual(a['metadata']['ai_reply_settings']['max_bargain_rounds'], 0)

    def test_incompatible_password_hash_never_reused_as_live_password(self):
        u = self.steps('xy_users')[0].values
        self.assertEqual(u['status'], 'INACTIVE')
        self.assertTrue(u['password_hash'].startswith('$pbkdf2-sha256$'))
        self.assertNotEqual(u['password_hash'], 'a'*64)
        self.assertIn('password_reset_required', str(self.build().issues))

    def test_default_empty_not_promoted_and_exclusive_not_misclassified(self):
        defaults = self.steps('xy_default_replies')
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0].values['reply_content'], '')
        self.assertTrue(defaults[0].values['reply_once'])
        issues = self.build().report()['issues']
        self.assertEqual(self.steps('xy_exclusive_replies')[0].values['content'], 'exclusive')
        self.assertEqual(self.steps('xy_reply_once_slots')[0].values['status'], 'confirmed')

    def test_chat_id_shared_across_accounts_stays_isolated(self):
        rows = self.steps('xy_ai_chat_messages')
        self.assertEqual({r.values['cookie_id'] for r in rows}, {'account-a','account-b'})
        self.assertEqual(len(rows), 2)
        self.assertIn('manual promise', [s.values['content'] for s in self.steps('xy_reply_events')])

    def test_notification_owner_mismatch_fails_preflight(self):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE notification_channels SET user_id=2')
        self.assertIsNotNone(plan, 'target-model mapping not implemented')
        with self.assertRaisesRegex(MigrationError, 'cross_owner_reference'):
            self.build()

    def test_order_unknown_never_reenters_ready_to_ship_inventory_quarantined(self):
        order = self.steps('xy_orders')[0].values
        self.assertEqual(order['status'], 'pending_verification')
        self.assertTrue(order['card_only_delivered'])
        self.assertEqual(order['quantity'], 2)
        self.assertEqual(order['metadata']['migration']['platform_status'], 'pending')
        self.assertFalse(self.steps('xy_cards')[0].values['enabled'])
        self.assertTrue(any(i['table'] == 'data_card_reservations' and i['blocking'] for i in self.build().issues))

    def test_public_report_never_contains_plaintext_or_messages(self):
        report = json.dumps(self.build().report())
        for value in ['FAKE_', 'manual promise', 'buyer-a', 'saved-user']:
            self.assertNotIn(value, report)
        self.assertTrue(self.build().report()['blocked'])

    def test_unknown_columns_and_tables_are_explicitly_reported(self):
        with sqlite3.connect(self.path) as db:
            db.executescript("ALTER TABLE cookies ADD COLUMN new_secret TEXT; CREATE TABLE unknown_future(id INTEGER); INSERT INTO unknown_future VALUES(1);")
        p = self.build()
        self.assertTrue(any(i['code'] == 'unmapped_columns' and 'new_secret' in i['fields'] for i in p.issues))
        self.assertTrue(any(i['table'] == 'unknown_future' for i in p.issues))

    def test_missing_account_reference_is_error_before_any_write(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE orders SET cookie_id='absent'")
        self.assertIsNotNone(plan, 'target-model mapping not implemented')
        with self.assertRaisesRegex(MigrationError, 'missing_account_reference'):
            self.build()

    def test_secret_metadata_not_duplicated_into_public_account_or_order_metadata(self):
        a = self.steps('xy_accounts')[0].values
        metadata = dict(a['metadata'])
        metadata.pop('ai_reply_settings')  # dedicated AI secret path has existing masking contract
        self.assertNotIn('FAKE_', json.dumps(metadata))
        self.assertNotIn('FAKE_RESERVED_CARD', json.dumps(self.steps('xy_orders')[0].values['metadata']))

    def test_encrypted_source_credentials_are_flagged_for_adapter(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE cookies SET value='enc$FAKE_CIPHERTEXT'")
        self.assertTrue(any(i['code'] == 'encrypted_legacy_secret_requires_adapter' for i in self.build().issues))

    def test_presets_and_blacklist_map_to_real_owner_scoped_models(self):
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE ai_config_presets(id INTEGER PRIMARY KEY,user_id INTEGER,preset_name TEXT,model_name TEXT,api_key TEXT,base_url TEXT,api_type TEXT);
            INSERT INTO ai_config_presets VALUES(1,1,'my preset','model','FAKE_KEY','https://ai.invalid','openai');
            CREATE TABLE xy_personal_blacklist(id INTEGER PRIMARY KEY,user_id INTEGER,cookie_id TEXT,buyer_id TEXT,buyer_nick TEXT,item_id TEXT,reason TEXT,is_enabled INTEGER);
            INSERT INTO xy_personal_blacklist VALUES(1,1,'account-a','blocked-buyer','','item-a','',0);''')
        preset = self.steps('xy_ai_presets')[0].values
        self.assertEqual(preset['settings_json']['provider_type'], 'openai_compatible')
        blocked = self.steps('xy_personal_blacklist')[0].values
        self.assertFalse(blocked['is_enabled'])
        self.assertEqual(blocked['account_id'], 'account-a')
        self.assertEqual(blocked['owner_id'], self.steps('xy_accounts')[0].values['owner_id'])

    def test_reservations_create_unknown_existing_delivery_intent_without_resending(self):
        intent = self.steps('xy_delivery_intents')[0].values
        self.assertEqual(intent['operation_key'], 'payment')
        self.assertEqual(intent['content_state'], 'unknown')
        self.assertEqual(intent['confirm_state'], 'unknown')
        self.assertEqual(intent['reserved_lines'], ['FAKE_RESERVED_CARD'])
        self.assertEqual(intent['quantity'], 2)

    def test_catalog_flags_are_native_fields_not_only_archived(self):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE item_info SET is_multi_spec=1, multi_quantity_delivery=1')
        metadata = self.steps('xy_catalog_items')[0].values['metadata']
        self.assertTrue(metadata.get('is_multi_spec'))
        self.assertTrue(metadata.get('multi_quantity_delivery'))

    def test_filter_and_supported_notification_template_have_real_consumers(self):
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE xy_message_filter_rules(id INTEGER PRIMARY KEY,user_id INTEGER,cookie_id TEXT,item_id TEXT,name TEXT,match_type TEXT,patterns TEXT,message_source TEXT,is_enabled INTEGER,action_skip_auto_reply INTEGER,action_skip_ai_reply INTEGER,action_pause_minutes INTEGER,action_notify INTEGER);
            INSERT INTO xy_message_filter_rules VALUES(1,1,'account-a',NULL,'fixture','exact','["ignore"]','user',1,1,0,0,0);
            CREATE TABLE notification_templates(id INTEGER PRIMARY KEY,type TEXT,template TEXT);
            INSERT INTO notification_templates VALUES(1,'message','{account_id}: {summary}');''')
        filters = self.steps('xy_reply_filters')
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].values['actions'], ['skip_reply'])
        self.assertEqual(filters[0].values['match_mode'], 'exact')
        templates = self.steps('xy_notify_templates')
        self.assertEqual(len(templates), 3)
        self.assertEqual(templates[0].values['body'], '{account_id}: {summary}')

    def test_known_buyer_template_variables_have_native_consumers(self):
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE notification_templates(id INTEGER PRIMARY KEY,type TEXT,template TEXT);
            INSERT INTO notification_templates VALUES(1,'message','{buyer_name}: {message}');''')
        self.assertFalse(any(i['code'] == 'notification_template_semantics_required' for i in self.build().issues))
        self.assertEqual({s.values['owner_id'] for s in self.steps('xy_notify_templates')}.__contains__(0), True)

    def test_material_skus_zero_stock_and_publishing_unknown_log_keep_links(self):
        sku = {'enabled': True, 'properties': [{'name': '颜色', 'support_image': False, 'values': [{'value': '红'}, {'value': '蓝'}]}],
               'items': [{'values': ['红'], 'price': 2, 'quantity': 0}, {'values': ['蓝'], 'price': 3, 'quantity': 4}]}
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE product_materials(id INTEGER PRIMARY KEY,user_id INTEGER,title TEXT,description TEXT,price REAL,images TEXT,delivery_method TEXT,postage REAL,can_self_pickup INTEGER,sku_config TEXT);
            CREATE TABLE publish_logs(id INTEGER PRIMARY KEY,user_id INTEGER,account_id TEXT,title TEXT,description TEXT,price TEXT,material_id INTEGER,batch_id TEXT,status TEXT,item_url TEXT,item_id TEXT,error_message TEXT);''')
            db.execute('INSERT INTO product_materials VALUES(1,1,?,?,?,?,?,?,?,?)', ('material','desc',2,'[]','包邮',0,0,json.dumps(sku)))
            db.execute("INSERT INTO publish_logs VALUES(1,1,'account-a','title','desc','2',1,'batch','publishing',NULL,NULL,'FAKE_SECRET_IN_ERROR')")
        material = self.steps('xy_product_materials')[0].values
        self.assertEqual(material['sku_rows'][0]['stock'], 0)
        self.assertEqual(material['sku_rows'][0]['specs'], {'颜色': '红'})
        log = self.steps('xy_publish_logs')[0].values
        self.assertEqual(log['material_id'], material['id'])
        self.assertEqual(log['status'], 'unknown')
        self.assertNotIn('FAKE_', json.dumps(log))

    def test_source_text_over_target_limit_fails_preflight(self):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE users SET username=? WHERE id=1', ('x' * 65,))
        with self.assertRaisesRegex(MigrationError, 'target_value_too_long'):
            self.build()

    def test_unknown_secret_fields_stay_only_in_encrypted_source_archive(self):
        with sqlite3.connect(self.path) as db:
            db.executescript("ALTER TABLE cookies ADD COLUMN future_secret TEXT; UPDATE cookies SET future_secret='FAKE_FUTURE_SECRET'; ALTER TABLE orders ADD COLUMN future_secret TEXT; UPDATE orders SET future_secret='FAKE_FUTURE_SECRET'; ALTER TABLE item_info ADD COLUMN future_secret TEXT; UPDATE item_info SET future_secret='FAKE_FUTURE_SECRET';")
        for table in ('xy_accounts', 'xy_orders', 'xy_catalog_items'):
            self.assertNotIn('FAKE_FUTURE_SECRET', json.dumps(self.steps(table)[0].values['metadata']))

    def test_empty_global_blacklist_account_scope_does_not_become_missing_account(self):
        with sqlite3.connect(self.path) as db:
            db.executescript("CREATE TABLE xy_personal_blacklist(id INTEGER PRIMARY KEY,user_id INTEGER,cookie_id TEXT,buyer_id TEXT,is_enabled INTEGER); INSERT INTO xy_personal_blacklist VALUES(1,1,'','global-buyer',1);")
        self.assertIsNone(self.steps('xy_personal_blacklist')[0].values['account_id'])
