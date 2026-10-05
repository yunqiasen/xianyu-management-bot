import unittest
from utils.notification_dispatcher import format_notification_template
class TemplateCompatibility(unittest.TestCase):
    def test_upstream_double_braces_and_legacy_single_braces(self):
        self.assertEqual(format_notification_template('{{ account_id }} / {account_id}',account_id='acct'),'acct / acct')
    def test_message_text_is_not_recursively_interpolated(self):
        self.assertEqual(format_notification_template('{message}',message='{account_id}',account_id='acct'),'{account_id}')
    def test_unknown_legacy_placeholder_is_preserved(self):
        self.assertEqual(format_notification_template('{future}',account_id='acct'),'{future}')
if __name__=='__main__': unittest.main()
