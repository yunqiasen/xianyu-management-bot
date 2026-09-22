import unittest
from datetime import datetime, timezone
from stdlib_loader import load

class PolishScheduleTests(unittest.TestCase):
    def decision(self, now, **kw):
        m=load('common/services/product_polish_schedule.py')
        return m.polish_window(datetime.fromisoformat(now), timezone_name=kw.get('timezone_name','Asia/Shanghai'),
            start=kw.get('start','23:00'), end=kw.get('end','01:00'), last_cycle=kw.get('last_cycle'))

    def test_cross_midnight_same_cycle(self):
        a=self.decision('2026-09-21T15:30:00+00:00')
        b=self.decision('2026-09-21T16:30:00+00:00')
        self.assertEqual(a['cycle'],b['cycle']); self.assertEqual(a['status'],'ready')

    def test_restart_after_window_skips_not_catchup(self):
        r=self.decision('2026-09-21T18:00:00+00:00')
        self.assertEqual(r['status'],'outside_window'); self.assertIn('next_run_at',r)

    def test_already_claimed_cycle_not_repeated(self):
        first=self.decision('2026-09-21T15:30:00+00:00')
        r=self.decision('2026-09-21T16:30:00+00:00',last_cycle=first['cycle'])
        self.assertEqual(r['status'],'already_processed')

    def test_timezone_and_bad_window_validation(self):
        with self.assertRaises(ValueError): self.decision('2026-09-21T15:30:00+00:00',start='bad')
        with self.assertRaises(ValueError): self.decision('2026-09-21T15:30:00+00:00',timezone_name='missing')
