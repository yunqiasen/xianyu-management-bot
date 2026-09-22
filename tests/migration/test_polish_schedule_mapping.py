"""S3 source daily clock -> native persistent window, without enabling execution."""
import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
import test_continuation as fixtures

class ScheduleMigrationTests(unittest.TestCase):
    setUp=fixtures.ContinuationTests.setUp
    prepare=fixtures.ContinuationTests.prepare
    apply=fixtures.ContinuationTests.apply
    rows=fixtures.ContinuationTests.rows

    def test_daily_hour_not_minutes_and_preserves_random_due_and_switch(self):
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE scheduled_tasks(id INTEGER PRIMARY KEY,name TEXT,task_type TEXT,account_id TEXT,enabled INTEGER,interval_hours INTEGER,delay_minutes INTEGER,random_delay_max INTEGER,next_run_at TEXT,last_run_at TEXT,last_run_result TEXT,user_id INTEGER);
            INSERT INTO scheduled_tasks VALUES(1,'每日擦亮','item_polish','account-a',1,24,9,10,'2026-09-24 09:07:00','2026-09-23 09:04:00','{"success":true}',1);''')
        prepared=self.prepare();self.apply(prepared)
        self.assertFalse(any(i['table']=='scheduled_tasks' for i in prepared.issues),prepared.issues)
        schedule=self.rows('xy_product_polish_schedules')[0]
        self.assertEqual((schedule['start'],schedule['end'],schedule['randomize']),('09:00','09:11',True))
        self.assertEqual(schedule['planned_at'],int(datetime(2026,9,24,9,7,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()))
        self.assertEqual(schedule['planned_cycle'],'Asia/Shanghai:2026-09-24')
        self.assertEqual(schedule['last_cycle'],'Asia/Shanghai:2026-09-23')
        account=next(r for r in self.rows('xy_accounts') if r['account_id']=='account-a')
        self.assertTrue(account['auto_polish']);self.assertEqual(account['status'],'disabled')
        self.assertEqual(self.runner.apply(prepared,quarantine=True)['inserted'],0)
