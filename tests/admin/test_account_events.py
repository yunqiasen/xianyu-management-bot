from support import *
from common.models.account_login_log import XYAccountLoginLog
from datetime import datetime,timedelta
class AccountEventsTests(DatabaseCase):
    async def test_summary_keeps_failure_skip_and_verification_separate(self):
        from app.services.account_login_log_service import AccountLoginLogService
        svc=AccountLoginLogService(self.session)
        self.assertTrue(callable(getattr(svc,'event_summary',None)))
        async with self.engine.begin() as conn:await conn.run_sync(XYAccountLoginLog.__table__.create)
        now=datetime.now()
        for id_,owner,reason,state in [(1,1,'rate_limited','failed'),(2,1,'bad_credentials','failed'),(3,1,'baxia_punish_captcha','failed'),(4,1,'rate_limited','skipped_cooldown'),(5,2,'bad_credentials','failed')]:
            self.session.add(XYAccountLoginLog(id=id_,owner_id=owner,account_identifier='account-a',login_status=state,failure_reason=reason,created_at=now))
        await self.session.commit()
        rows=await svc.event_summary(1,now-timedelta(days=1),now+timedelta(days=1))
        self.assertEqual(sum(r['count'] for r in rows),4)
        self.assertEqual({r['category'] for r in rows},{'rate_limit','credentials','manual_verification'})
        self.assertEqual(sum(r['count'] for r in rows if r['status']=='skipped_cooldown'),1)
