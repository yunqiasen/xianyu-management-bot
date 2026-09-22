import unittest
from common.db.session import _compile_sql_with_params
from common.utils.security import create_access_token,decode_token

class SharedPrivacyTests(unittest.TestCase):
    def test_token_type_is_explicit_and_cannot_be_overridden(self):
        self.assertEqual(decode_token(create_access_token('1')).get('type'),'access')
        self.assertEqual(decode_token(create_access_token({'sub':'1','type':'refresh'}))['type'],'access')
    def test_sql_diagnostics_do_not_expand_secrets(self):
        for sql,params in [
            ('UPDATE xy_accounts SET metadata=:value',{'value':'{"api_key":"fixture-key"}'}),
            ('INSERT INTO xy_cards VALUES (%s)',('fixture-card-content',)),
            ("UPDATE xy_accounts SET cookie='fixture-inline-cookie'",{}),
        ]:
            rendered=_compile_sql_with_params(sql,params)
            self.assertNotIn('fixture-',rendered)
