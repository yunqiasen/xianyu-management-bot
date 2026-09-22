import unittest
from types import SimpleNamespace
from stdlib_loader import load

class FeedbackTests(unittest.TestCase):
    def decision(self, kind='rate', **kw):
        m=load('common/services/product_feedback_policy.py')
        account=SimpleNamespace(id=1, owner_id=9, status='active')
        order=SimpleNamespace(account_pk=kw.get('account_pk',1),owner_id=kw.get('owner_id',9),
            status=kw.get('status','交易成功'), is_rated=kw.get('is_rated',False),is_red_flower=kw.get('is_red_flower',False))
        return m.feedback_eligibility(account,order,kind=kind,cooling=kw.get('cooling',False))
    def test_other_account_or_owner_not_eligible(self):
        self.assertEqual(self.decision(account_pk=2),'account_mismatch')
        self.assertEqual(self.decision(owner_id=8),'account_mismatch')
    def test_refund_and_unpaid_excluded(self):
        for status in ['待付款','退款成功','交易关闭','unknown']:
            self.assertEqual(self.decision(status=status),'not_applicable')
    def test_done_and_cooldown_have_distinct_reasons(self):
        self.assertEqual(self.decision(is_rated=True),'already_processed')
        self.assertEqual(self.decision(kind='red_flower',is_red_flower=True),'already_processed')
        self.assertEqual(self.decision(cooling=True),'cooldown')
    def test_complete_order_is_ready(self): self.assertEqual(self.decision(),'ready')
