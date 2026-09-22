from pathlib import Path
import unittest

class AIMountTests(unittest.TestCase):
    def test_accounts_modal_has_only_new_editor(self):
        source = (Path(__file__).parents[2] / 'frontend/src/pages/accounts/Accounts.tsx').read_text()
        self.assertIn('<AISettingsPanel', source)
        self.assertNotIn('handleSaveAISettings', source)
        self.assertNotIn('handleTestAI', source)

    def test_accounts_human_controls_are_mounted(self):
        source=(Path(__file__).parents[2]/'frontend/src/pages/accounts/Accounts.tsx').read_text()
        self.assertTrue('<AccountVerification' in source)
        self.assertTrue('startAccountVerification(account.id)' in source)
