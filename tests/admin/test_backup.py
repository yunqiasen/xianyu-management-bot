from support import *
import gzip,tempfile
class BackupTests(unittest.TestCase):
    def test_sql_gzip_integrity_and_forbidden_cross_database(self):
        try:
            from app.services.backup_verification_service import inspect_backup
        except ImportError: self.fail('缺SQL.gz校验与隔离恢复入口')
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'sample.sql.gz'
            with gzip.open(path,'wt') as f: f.write("SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\nCREATE TABLE `demo` (`id` int PRIMARY KEY, `v` text);\nINSERT INTO `demo` (`id`,`v`) VALUES (1,'a;b');\nSET FOREIGN_KEY_CHECKS=1;\n")
            data=inspect_backup(path)
            self.assertEqual(data['tables'],['demo']); self.assertEqual(data['rows'],{'demo':1})
            with gzip.open(path,'wt') as f:f.write('USE production; DROP TABLE `x`;')
            with self.assertRaises(ValueError):inspect_backup(path)
            path.write_bytes(b'broken')
            with self.assertRaises(ValueError):inspect_backup(path)
    def test_retention_requires_latest_restore_and_keeps_protected(self):
        try: from common.services.backup_retention_service import retention_candidates
        except ImportError: self.fail('缺恢复验证后的日周保留策略')
        from types import SimpleNamespace
        from datetime import datetime,timedelta
        logs=[SimpleNamespace(id=n,created_at=datetime(2026,9,21)-timedelta(days=n),status='success') for n in range(50)]
        checks=[SimpleNamespace(backup_log_id=1,status='restored',protected=False)]
        self.assertEqual(retention_candidates(logs,checks),[])
        checks+=[SimpleNamespace(backup_log_id=0,status='restored',protected=False),SimpleNamespace(backup_log_id=49,status='checked',protected=True)]
        candidates=retention_candidates(logs,checks)
        self.assertTrue(candidates);self.assertNotIn(49,candidates);self.assertNotIn(0,candidates)
        self.assertTrue(set(range(7)).isdisjoint(candidates))

class BackupAPITests(DatabaseCase):
    async def test_real_backup_record_verify_api_and_missing_restore_engine(self):
        from common.models.db_backup_log import DbBackupLog
        from app.api.routes.db_backup_logs import router
        from app.api import deps
        from fastapi import FastAPI
        from types import SimpleNamespace
        from unittest.mock import patch
        import httpx
        async with self.engine.begin() as conn:await conn.run_sync(DbBackupLog.__table__.create)
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'fixture.sql.gz'
            with gzip.open(path,'wt') as f:f.write('CREATE TABLE `x` (`id` int);\nINSERT INTO `x` (`id`) VALUES (1);\n')
            self.session.add(DbBackupLog(id=1,status='success',file_name=path.name,table_count=1,total_rows=1));await self.session.commit()
            app=FastAPI();app.include_router(router)
            app.dependency_overrides[deps.get_current_admin_user]=lambda:SimpleNamespace(id=1)
            app.dependency_overrides[deps.get_db_session]=lambda:self.session
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as c:
                with patch('common.utils.backup_paths.get_backup_root',return_value=Path(root)):
                    r=await c.post('/db-backup-logs/1/verify')
                    self.assertEqual(r.status_code,200);self.assertEqual(r.json()['data']['status'],'checked')
                    self.assertEqual((await c.get('/db-backup-logs/1/verifications')).json()['data'][0]['sha256'],r.json()['data']['sha256'])
                    self.assertEqual((await c.post('/db-backup-logs/1/verify?restore=true')).status_code,409)
                    path.write_bytes(b'broken')
                    self.assertEqual((await c.post('/db-backup-logs/1/verify')).status_code,422)
