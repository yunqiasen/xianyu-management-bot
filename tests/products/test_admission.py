import unittest
from types import SimpleNamespace
from stdlib_loader import load

class ProductAdmissionTests(unittest.TestCase):
    def test_recovery_pause_proxy_and_retry_wait_block_all_product_work(self):
        module=load('common/services/product_admission.py')
        for state in ['recovering','verification_required','proxy_error','paused']:
            account=SimpleNamespace(status='active', metadata_json={'xy_runtime':{'business_state':state,'next_retry_at':200}})
            decision=module.product_admission(account,now=100)
            self.assertFalse(decision['allowed']); self.assertEqual(decision['status'],state)
    def test_active_legacy_and_ready_work_but_disabled_does_not(self):
        module=load('common/services/product_admission.py')
        for status,meta,allowed in [('active',{},True),('inactive',{},False),('active',{'xy_runtime':{'business_state':'ready'}},True)]:
            self.assertEqual(module.product_admission(SimpleNamespace(status=status,metadata_json=meta),now=100)['allowed'],allowed)
