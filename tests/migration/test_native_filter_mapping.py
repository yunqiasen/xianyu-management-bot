import sqlite3
import unittest
import test_continuation as fixtures

class NativeFilterMappingTests(unittest.TestCase):
    setUp=fixtures.ContinuationTests.setUp
    prepare=fixtures.ContinuationTests.prepare
    apply=fixtures.ContinuationTests.apply
    rows=fixtures.ContinuationTests.rows

    def test_global_pause_filter_is_not_expanded_into_a_frozen_account_list(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE xy_message_filter_rules SET cookie_id=NULL,action_pause_minutes=7")
        prepared=self.prepare()
        self.assertFalse(any(i['table']=='xy_message_filter_rules' for i in prepared.issues),prepared.issues)
        self.apply(prepared)
        filters=self.rows('xy_reply_filters')
        self.assertEqual(len(filters),1)
        self.assertEqual(filters[0]['account_id'],'')
        self.assertEqual(filters[0]['pause_minutes'],7)
        self.assertIn('pause',filters[0]['actions'])
        self.assertEqual(filters[0]['owner_id'],next(r['owner_id'] for r in self.rows('xy_accounts') if r['account_id']=='account-a'))
