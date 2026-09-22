"""Source dynamic title predicates retain identity, priority and delivery multiplicity."""
import sqlite3
import unittest
import test_continuation as fixtures


class NativeRuleMappingTests(unittest.TestCase):
    setUp=fixtures.ContinuationTests.setUp
    prepare=fixtures.ContinuationTests.prepare
    apply=fixtures.ContinuationTests.apply
    rows=fixtures.ContinuationTests.rows

    def test_title_rule_is_native_and_not_limited_to_current_catalog(self):
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE delivery_rules(id INTEGER PRIMARY KEY,user_id INTEGER,keyword TEXT,card_id INTEGER,delivery_count INTEGER,enabled INTEGER,description TEXT,delivery_times INTEGER);
            INSERT INTO delivery_rules VALUES(15,1,'later product',1,3,1,'配置说明',7);''')
        prepared=self.prepare(); self.apply(prepared)
        rules=self.rows('xy_delivery_rules')
        self.assertEqual(len(rules),1)
        self.assertEqual((rules[0]['keyword'],rules[0]['delivery_count'],rules[0]['delivery_times'],rules[0]['legacy_id']),('later product',3,7,15))
        self.assertEqual(rules[0]['match_mode'],'legacy_contains')
        self.assertFalse(any(i['code'] in {'dynamic_title_rule_target_required','rule_delivery_count_target_required'} for i in prepared.issues))
        self.assertEqual(self.runner.apply(prepared,quarantine=True)['inserted'],0)

    def test_sqlite_like_wildcards_survive_as_native_legacy_rules(self):
        with sqlite3.connect(self.path) as db:
            db.executescript("""CREATE TABLE delivery_rules(id INTEGER PRIMARY KEY,user_id INTEGER,keyword TEXT,card_id INTEGER,delivery_count INTEGER,enabled INTEGER,description TEXT,delivery_times INTEGER);
            INSERT INTO delivery_rules VALUES(15,1,'A_%',1,1,1,'wildcard fixture',0);""")
        prepared=self.prepare(); self.apply(prepared)
        rule=self.rows('xy_delivery_rules')[0]
        self.assertEqual((rule['keyword'],rule['match_mode']),('A_%','legacy_contains'))
        self.assertNotIn('legacy_sql_wildcard_review',[issue['code'] for issue in prepared.issues])
