import importlib.util
import unittest


class ContractTests(unittest.TestCase):
    def test_dispatch_contract_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('common.services.account_dispatch'))

    def test_budget_contract_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('common.services.account_request_budget'))
