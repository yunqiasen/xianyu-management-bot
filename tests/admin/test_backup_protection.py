"""S1/S3: protect a verified file through the authenticated management API."""
import gzip
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from support import DatabaseCase, User, UserRole
from app.api import deps
from app.api.routes.db_backup_logs import router
from app.services.auth import AuthService
from common.models.admin_control import AdminAudit, BackupVerification
from common.models.db_backup_log import DbBackupLog
from common.services.backup_retention_service import retention_candidates


class BackupProtectionTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.engine.begin() as connection:
            await connection.run_sync(DbBackupLog.__table__.create)
        self.temp = tempfile.TemporaryDirectory(prefix='xymb-protected-backup-')
        self.root = Path(self.temp.name)
        self.path = self.root / 'cutover.sql.gz'
        with gzip.open(self.path, 'wt') as stream:
            stream.write('CREATE TABLE `fixture` (`id` INTEGER);\nINSERT INTO `fixture` (`id`) VALUES (1);\n')
        admin = await self.session.get(User, 1)
        admin.role = UserRole.ADMIN
        self.session.add(DbBackupLog(id=1, status='success', file_name=self.path.name,
            table_count=1, total_rows=1, created_at=datetime(2026, 1, 1)))
        await self.session.commit()
        auth = AuthService(self.session)
        self.admin = {'Authorization': 'Bearer ' + auth.create_access_token(admin)}
        self.member = {'Authorization': 'Bearer ' + auth.create_access_token(await self.session.get(User, 2))}
        app = FastAPI()
        app.include_router(router, prefix='/api/v1')
        app.dependency_overrides[deps.get_db_session] = lambda: self.session
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture')
        self.root_patch = patch('common.utils.backup_paths.get_backup_root', return_value=self.root)
        self.root_patch.start()
        result = await self.client.post('/api/v1/db-backup-logs/1/verify', headers=self.admin)
        self.assertEqual(result.status_code, 200, result.text)
        self.check_id = result.json()['data']['id']
        self.url = f'/api/v1/db-backup-logs/1/verifications/{self.check_id}/protection'

    async def asyncTearDown(self):
        self.root_patch.stop()
        await self.client.aclose()
        self.temp.cleanup()
        await super().asyncTearDown()

    async def test_admin_can_protect_and_unprotect_a_verified_cutover_backup(self):
        response = await self.client.put(self.url, json={'protected': True}, headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()['data']['protected'], True)
        # Repeated set is idempotent; retained old verification remains visible after newer checks.
        await self.client.put(self.url, json={'protected': True}, headers=self.admin)
        for _ in range(21):
            await self.client.post('/api/v1/db-backup-logs/1/verify', headers=self.admin)
        rows = (await self.client.get('/api/v1/db-backup-logs/1/verifications', headers=self.admin)).json()['data']
        self.assertTrue(any(row['id'] == self.check_id and row['protected'] for row in rows))
        changes = (await self.session.scalars(select(AdminAudit).where(AdminAudit.action == 'backup_protection'))).all()
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].actor_id, 1)
        self.assertEqual(changes[0].details, {'verification_id': self.check_id, 'protected': True})
        # The real retention policy keeps the marked point even outside all time buckets.
        logs = [DbBackupLog(id=n, status='success', created_at=datetime(2026, 9, 23)-timedelta(days=n))
                for n in range(2, 40)] + [await self.session.get(DbBackupLog, 1)]
        newest = BackupVerification(backup_log_id=2, status='restored', protected=False)
        checks = [newest, await self.session.get(BackupVerification, self.check_id)]
        self.assertNotIn(1, retention_candidates(logs, checks))
        response = await self.client.put(self.url, json={'protected': False}, headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()['data']['protected'], False)
        self.assertIn(1, retention_candidates(logs, checks))

    async def test_permissions_checksum_binding_and_strict_boolean(self):
        for headers, code in (({}, 401), (self.member, 403)):
            response = await self.client.put(self.url, json={'protected': True}, headers=headers)
            self.assertEqual(response.status_code, code, response.text)
        response = await self.client.put(self.url, json={'protected': 'false'}, headers=self.admin)
        self.assertEqual(response.status_code, 422)
        wrong_parent = self.url.replace('/1/verifications/', '/999/verifications/')
        self.assertEqual((await self.client.put(wrong_parent, json={'protected': True}, headers=self.admin)).status_code, 404)
        self.path.write_bytes(b'changed-file')
        response = await self.client.put(self.url, json={'protected': True}, headers=self.admin)
        self.assertEqual(response.status_code, 409, response.text)
        rows = (await self.client.get('/api/v1/db-backup-logs/1/verifications', headers=self.admin)).json()['data']
        self.assertFalse(rows[0]['protected'])
