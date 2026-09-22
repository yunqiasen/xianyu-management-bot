from support import *
from datetime import datetime,timedelta
from common.models.account_login_log import XYAccountLoginLog
from common.models.risk_control_log import XYRiskControlLog
class ArchiveTests(DatabaseCase):
    async def test_clear_archives_terminal_but_preserves_unknown_and_recent(self):
        from app.services.account_login_log_service import AccountLoginLogService
        async with self.engine.begin() as conn:await conn.run_sync(XYAccountLoginLog.__table__.create)
        old=datetime.now()-timedelta(days=40)
        self.session.add_all([XYAccountLoginLog(id=1,owner_id=1,account_identifier='account-a',login_status='success',created_at=old),XYAccountLoginLog(id=2,owner_id=1,account_identifier='account-a',login_status='unknown',created_at=old),XYAccountLoginLog(id=3,owner_id=1,account_identifier='account-a',login_status='failed',created_at=datetime.now())]);await self.session.commit()
        removed=await AccountLoginLogService(self.session).cleanup_all_logs()
        self.assertEqual(removed,1)
        self.assertIsNotNone(await self.session.get(XYAccountLoginLog,2));self.assertIsNotNone(await self.session.get(XYAccountLoginLog,3))
        try: from common.models.admin_control import AdminLogArchive
        except ImportError:self.fail('缺持久归档证据')
        from sqlalchemy import select
        archive=(await self.session.execute(select(AdminLogArchive))).scalar_one();self.assertEqual(archive.source_id,1)
