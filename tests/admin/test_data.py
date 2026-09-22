from support import *
from app.api.routes import admin
from app.api import deps
from fastapi import FastAPI
from types import SimpleNamespace
import httpx
class DataTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        app=FastAPI(); app.include_router(admin.router,prefix='/admin')
        app.dependency_overrides[deps.get_current_admin_user]=lambda: SimpleNamespace(id=1,role=UserRole.ADMIN)
        app.dependency_overrides[deps.get_db_session]=lambda: self.session
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture')
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from common.models.db_backup_log import DbBackupLog
        async with self.engine.begin() as connection:
            await connection.run_sync(DbBackupLog.__table__.create)
        self.temp = tempfile.TemporaryDirectory(prefix='xymb-data-gate-')
        self.backup_root = Path(self.temp.name)
        self.root_patch = patch('common.utils.backup_paths.get_backup_root', return_value=self.backup_root)
        self.root_patch.start()
    async def asyncTearDown(self):
        self.root_patch.stop(); self.temp.cleanup()
        await self.client.aclose(); await super().asyncTearDown()
    async def test_accounts_paginated_and_secrets_not_exported(self):
        res=await self.client.get('/admin/data/xy_accounts?limit=1')
        self.assertNotIn('fixture-secret',res.text)
        self.assertEqual(len(res.json()['data']),1)
        self.assertEqual(res.json()['count'],2)
    async def test_unknown_table_is_error_not_empty_success(self):
        res=await self.client.get('/admin/data/not_allowed')
        self.assertEqual(res.status_code,404)
    async def test_delete_requires_preview_and_confirmation(self):
        res=await self.client.delete('/admin/data/xy_accounts/1')
        self.assertIn(res.status_code,[403,409,422])
    async def test_preview_delete_atomic_related_and_audit(self):
        from common.models.admin_control import BackupVerification,AdminAudit
        from sqlalchemy import select
        self.session.add_all([NotificationChannel(id=11,owner_id=1,name='channel',channel_type='webhook',config_payload={'webhook_url':'http://fixture.test'}),MessageNotification(id=11,owner_id=1,account_pk=1,account_identifier='account-a',channel_id=11)])
        await self.session.commit()
        preview=(await self.client.post('/admin/data/notification_channels/preview')).json()
        self.assertEqual((preview['count'],preview['related_count']),(1,1))
        url='/admin/data/notification_channels?preview_id='+preview['preview_id']+'&confirmation=DELETE'
        self.assertEqual((await self.client.delete(url)).status_code,409)
        await self.verified_fixture()
        response=await self.client.delete(url)
        self.assertEqual(response.status_code,200);self.assertTrue(response.json()['success'])
        self.assertIsNone(await self.session.get(MessageNotification,11))
        audit=(await self.session.execute(select(AdminAudit).where(AdminAudit.action=='data_delete'))).scalar_one()
        self.assertEqual(audit.details['related_count'],1)
        self.assertEqual((await self.client.delete(url)).status_code,409)
    async def test_preview_becomes_stale_after_concurrent_edit(self):
        from common.models.admin_control import BackupVerification
        channel=NotificationChannel(id=12,owner_id=1,name='before',channel_type='webhook',config_payload={})
        self.session.add(channel); await self.session.commit(); await self.verified_fixture()
        preview=(await self.client.post('/admin/data/notification_channels/preview')).json()
        channel.name='after';await self.session.commit()
        r=await self.client.delete('/admin/data/notification_channels?preview_id='+preview['preview_id']+'&confirmation=DELETE')
        self.assertEqual(r.status_code,409)
        self.assertIsNotNone(await self.session.get(NotificationChannel,12))
    async def test_restore_of_unrelated_tables_does_not_open_delete_gate(self):
        self.session.add_all([
            NotificationChannel(id=13,owner_id=1,name='keep',channel_type='webhook',config_payload={}),
            BackupVerification(backup_log_id=1,sha256='b'*64,status='restored',
                report={'isolated':True,'restored_rows':{'unrelated_fixture':1}}),
        ])
        await self.session.commit()
        preview=(await self.client.post('/admin/data/notification_channels/preview')).json()
        response=await self.client.delete('/admin/data/notification_channels?preview_id='+preview['preview_id']+'&confirmation=DELETE')
        self.assertEqual(response.status_code,409,response.text)
        self.assertIsNotNone(await self.session.get(NotificationChannel,13))

    async def verified_fixture(self):
        """Seed a pre-verified recovery point; browser_flow exercises real MySQL restoration."""
        import gzip
        from common.models.db_backup_log import DbBackupLog
        from common.services.data_snapshot import data_fingerprint
        from sqlalchemy import select
        file = self.backup_root / 'verified.sql.gz'
        with gzip.open(file, 'wt') as stream:
            stream.write('fixture recovery content')
        checks = {}
        for model in (NotificationChannel, MessageNotification):
            checks[model.__tablename__] = data_fingerprint((await self.session.execute(select(model.__table__))).mappings().all())
        self.session.add(DbBackupLog(id=1, status='success', file_name=file.name))
        self.session.add(BackupVerification(backup_log_id=1, sha256=sha256(file.read_bytes()).hexdigest(), status='restored',
            report={'isolated':True, 'table_fingerprints':checks}))
        await self.session.commit()
        return file

    async def test_backup_from_before_a_configuration_change_is_not_a_current_recovery_point(self):
        channel=NotificationChannel(id=14,owner_id=1,name='old',channel_type='webhook',config_payload={})
        self.session.add(channel); await self.session.commit(); await self.verified_fixture()
        channel.name='new'; await self.session.commit()
        preview=(await self.client.post('/admin/data/notification_channels/preview')).json()
        response=await self.client.delete('/admin/data/notification_channels?preview_id='+preview['preview_id']+'&confirmation=DELETE')
        self.assertEqual(response.status_code,409,response.text)
        self.assertIsNotNone(await self.session.get(NotificationChannel,14))

    async def test_changed_backup_file_does_not_open_delete_gate(self):
        self.session.add(NotificationChannel(id=15,owner_id=1,name='keep',channel_type='webhook',config_payload={}))
        await self.session.commit(); file = await self.verified_fixture()
        file.write_bytes(b'corrupted')
        preview=(await self.client.post('/admin/data/notification_channels/preview')).json()
        response=await self.client.delete('/admin/data/notification_channels?preview_id='+preview['preview_id']+'&confirmation=DELETE')
        self.assertEqual(response.status_code,409,response.text)
        self.assertIsNotNone(await self.session.get(NotificationChannel,15))
