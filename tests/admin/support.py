import os
import sys
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend-web'))
os.environ['SQL_ECHO'] = 'false'
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy import BigInteger
@compiles(BigInteger, 'sqlite')
def bigint_sqlite(type_, compiler, **kw):
    return 'INTEGER'
from common.db.base_class import Base
from common.models.user import User, UserRole, UserStatus
from common.models.xy_account import XYAccount
from common.models.notification_channel import NotificationChannel
from common.models.message_notification import MessageNotification
from common.models.xy_order import XYOrder
from hashlib import sha256
class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as conn:
            for table in list(Base.metadata.tables.values()):
                if table.name in {'xy_users','xy_accounts','xy_notification_channels','xy_message_notifications','xy_orders'} or table.name.startswith('xy_admin_') or table.name.startswith('xy_notify_'):
                    await conn.run_sync(table.create)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([User(id=1,username='seller',email='seller@example.com',password_hash=sha256(b'correct').hexdigest()), User(id=2,username='other',email='other@example.com',password_hash=sha256(b'correct').hexdigest())])
        self.session.add_all([XYAccount(id=1,owner_id=1,account_id='account-a',cookie='fixture-secret',login_method='manual'), XYAccount(id=2,owner_id=2,account_id='account-b',cookie='fixture-secret-b',login_method='manual')])
        await self.session.commit()
    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
from common.models.admin_control import LoginProtection, AdminAudit, BackupVerification
from common.models.notification_delivery import NotificationEvent, NotificationDelivery, NotificationTemplate
