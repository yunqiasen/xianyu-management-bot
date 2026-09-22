"""S1/S3: scheduled maintenance moves old terminal audits without dropping evidence."""
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.api import deps
from app.api.routes import admin
from app.services.auth import AuthService
from common.db.fork_schema import upgrade_fork_schema
from common.models.admin_control import AdminAudit
from common.models.db_backup_log import DbBackupLog
from common.models.user import User, UserRole
from tools.verification.run import commerce_fixture


class AuditRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_maintenance_archives_old_completed_audits_and_keeps_unknown(self):
        module_path = Path(__file__).resolve().parents[2] / 'scheduler/app/services/scheduler/db_backup_task.py'
        spec = importlib.util.spec_from_file_location('audit_retention_exporter', module_path)
        exporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(exporter)
        with tempfile.TemporaryDirectory(prefix='xymb-audit-retention-') as temp, commerce_fixture() as url:
            engine = create_async_engine(url, hide_parameters=True)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            root = Path(temp)
            now = datetime(2026, 9, 23, 12)
            old, recent = now - timedelta(days=181), now - timedelta(days=179)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(User.__table__.create)
                    await conn.run_sync(DbBackupLog.__table__.create)
                    await conn.run_sync(upgrade_fork_schema)
                async with sessions() as session:
                    user = User(id=1, username='archive-admin', email='archive@example.com', role=UserRole.ADMIN, password_hash='fixture')
                    session.add_all([user,
                        AdminAudit(id='old-completed', actor_id=1, action='user_update', target='2',
                            details={'password': 'PRIVATE_FIXTURE', 'fields': ['role']}, created_at=old),
                        AdminAudit(id='recent', actor_id=1, action='user_update', target='2', details={}, created_at=recent),
                        AdminAudit(id='old-pending', actor_id=1, action='backup_restore_verify', target='3',
                            details={'status': 'restoring'}, created_at=old),
                        AdminAudit(id='old-unknown', actor_id=1, action='future_action', target='operation-1',
                            details={'status': 'unknown'}, created_at=old),
                    ])
                    await session.commit()
                    headers = {'Authorization': 'Bearer ' + AuthService(session).create_access_token(user)}
                app = FastAPI()
                app.include_router(admin.router, prefix='/api/v1/admin')
                async def db():
                    async with sessions() as session:
                        yield session
                app.dependency_overrides[deps.get_db_session] = db
                with patch.object(exporter, 'async_session_maker', sessions), \
                     patch.object(exporter, 'get_settings', return_value=SimpleNamespace(mysql_database=engine.url.database)), \
                     patch.object(exporter, 'ensure_backup_root', return_value=root), \
                     patch('common.utils.time_utils.get_beijing_now_naive', return_value=now), \
                     patch('common.utils.backup_paths.get_backup_root', return_value=root):
                    for _ in range(2):
                        await exporter.DbBackupTaskService().execute()
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                            response = await client.get('/api/v1/admin/log-archive?source_table=xy_admin_audit', headers=headers)
                            self.assertEqual(response.status_code, 200, response.text)
                            rows = response.json()['data']
                            self.assertEqual([row['source_id'] for row in rows], ['old-completed'], response.text)
                            self.assertNotIn('PRIVATE_FIXTURE', response.text)
                            self.assertEqual(rows[0]['payload']['actor_id'], 1)
                            self.assertEqual(rows[0]['payload']['created_at'], old.isoformat())
                        async with sessions() as session:
                            self.assertEqual(set(await session.scalars(select(AdminAudit.id))),
                                {'recent', 'old-pending', 'old-unknown'})
                            self.assertTrue(all(state == 'success' for state in await session.scalars(select(DbBackupLog.status))))
            finally:
                await engine.dispose()
