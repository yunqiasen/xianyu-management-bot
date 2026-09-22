"""S1/S3: the real scheduled exporter -> download -> actual second-MySQL restore."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from common.models.admin_control import AdminAudit, BackupVerification, AdminAuditArchive
from common.models.db_backup_log import DbBackupLog
from app.api import deps
from app.api.routes.db_backup_logs import router
from tools.verification.run import commerce_fixture
from browser_flow import restore_fixture


class BackupRoundtripTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_full_dump_preserves_logs_binary_text_precision_and_generated_fields(self):
        module_path=Path(__file__).resolve().parents[2]/'scheduler/app/services/scheduler/db_backup_task.py'
        spec=importlib.util.spec_from_file_location('backup_task_roundtrip',module_path)
        exporter=importlib.util.module_from_spec(spec); spec.loader.exec_module(exporter)
        with tempfile.TemporaryDirectory(prefix='xymb-backup-roundtrip-') as temp, commerce_fixture() as url:
            engine=create_async_engine(url,hide_parameters=True,connect_args={
                'init_command': "SET time_zone = '+08:00', sql_mode = CONCAT(@@sql_mode, ',NO_AUTO_VALUE_ON_ZERO')"})
            sessions=async_sessionmaker(engine,expire_on_commit=False)
            root=Path(temp)
            try:
                async with engine.begin() as conn:
                    for model in (DbBackupLog,BackupVerification,AdminAudit,AdminAuditArchive):
                        await conn.run_sync(model.__table__.create)
                    await conn.execute(text("""CREATE TABLE fixture_parent (
                        id INTEGER AUTO_INCREMENT PRIMARY KEY,body LONGTEXT,raw LONGBLOB,at DATETIME(6),
                        computed INTEGER GENERATED ALWAYS AS (OCTET_LENGTH(raw)) STORED,ts TIMESTAMP(6))"""))
                    await conn.execute(text("""CREATE TABLE fixture_event_logs (
                        id INTEGER PRIMARY KEY,parent_id INTEGER,status VARCHAR(32),
                        FOREIGN KEY(parent_id) REFERENCES fixture_parent(id))"""))
                    await conn.execute(text("""INSERT INTO fixture_parent (id,body,raw,at,ts)
                        VALUES(0,:body,:raw,'2026-09-23 10:11:12.123456','2026-09-23 10:11:12.123456')"""),
                        {'body':"fixture\x00 quote' slash\\\n-- still content 中文",'raw':b'\x00\xff\x01'})
                    await conn.execute(text("INSERT INTO fixture_event_logs VALUES(1,0,'unknown')"))
                    expected_parent=(await conn.execute(text("SELECT id,body,raw,at,computed,CONVERT_TZ(ts,'+08:00','+00:00') FROM fixture_parent"))).all()
                    expected_events=(await conn.execute(text('SELECT * FROM fixture_event_logs'))).all()
                with patch.object(exporter,'async_session_maker',sessions), \
                     patch.object(exporter,'get_settings',return_value=SimpleNamespace(mysql_database=engine.url.database)), \
                     patch.object(exporter,'ensure_backup_root',return_value=root), \
                     patch('common.utils.backup_paths.get_backup_root',return_value=root):
                    await exporter.DbBackupTaskService().execute()
                    async with sessions() as session:
                        record=await session.scalar(select(DbBackupLog).order_by(DbBackupLog.id.desc()))
                        self.assertEqual(record.status,'success')
                        backup_id=record.id
                    app=FastAPI();app.include_router(router,prefix='/api/v1')
                    async def db():
                        async with sessions() as session:yield session
                    app.dependency_overrides[deps.get_db_session]=db
                    app.dependency_overrides[deps.get_current_admin_user]=lambda:SimpleNamespace(id=7)
                    async with restore_fixture() as restored:
                        app.state.admin_restore_engine=restored
                        async with restored.connect() as conn:
                            original_settings=(await conn.execute(text('SELECT @@SESSION.sql_mode,@@SESSION.time_zone,@@SESSION.foreign_key_checks'))).one()
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                            download=await client.get(f'/api/v1/db-backup-logs/{backup_id}/download')
                            self.assertEqual(download.status_code,200)
                            self.assertEqual(download.headers['content-type'],'application/gzip')
                            self.assertEqual(download.content,(root/record.file_name).read_bytes())
                            response=await client.post(f'/api/v1/db-backup-logs/{backup_id}/verify?restore=true')
                            self.assertEqual(response.status_code,200,response.text)
                            self.assertEqual(response.json()['data']['status'],'restored',response.text)
                        async with restored.connect() as conn:
                            self.assertEqual((await conn.execute(text('SELECT * FROM fixture_parent'))).all(),expected_parent)
                            self.assertEqual((await conn.execute(text('SELECT * FROM fixture_event_logs'))).all(),expected_events)
                            self.assertEqual((await conn.execute(text('SELECT @@SESSION.sql_mode,@@SESSION.time_zone,@@SESSION.foreign_key_checks'))).one(),original_settings)
                    async with engine.connect() as conn:
                        self.assertEqual((await conn.execute(text('SELECT @@SESSION.time_zone'))).scalar_one(),'+08:00')
                    self.assertEqual((root/record.file_name).stat().st_mode & 0o777,0o600)
            finally:
                await engine.dispose()

    async def test_export_pins_one_read_snapshot_even_when_default_is_read_committed(self):
        from sqlalchemy import create_engine, event
        from sqlalchemy.engine import make_url
        from app.services.backup_verification_service import BackupVerificationService
        module_path=Path(__file__).resolve().parents[2]/'scheduler/app/services/scheduler/db_backup_task.py'
        spec=importlib.util.spec_from_file_location('backup_snapshot_roundtrip',module_path)
        exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)
        with tempfile.TemporaryDirectory(prefix='xymb-backup-snapshot-') as temp, commerce_fixture() as url:
            engine=create_async_engine(url,hide_parameters=True,isolation_level='READ COMMITTED')
            writer=create_engine(make_url(url).set(drivername='mysql+pymysql'),hide_parameters=True)
            sessions=async_sessionmaker(engine,expire_on_commit=False);root=Path(temp);writes=[]
            try:
                async with engine.begin() as conn:
                    for model in (DbBackupLog,BackupVerification,AdminAudit,AdminAuditArchive):await conn.run_sync(model.__table__.create)
                    for table in ('fixture_a','fixture_b'):
                        await conn.execute(text(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY,version INTEGER NOT NULL)'))
                        await conn.execute(text(f'INSERT INTO {table} VALUES(1,1)'))
                def update_between_reads(conn,cursor,statement,params,context,many):
                    if statement.startswith('SELECT') and 'FROM `fixture_b`' in statement and not writes:
                        with writer.begin() as other:
                            other.execute(text('UPDATE fixture_a SET version=2'))
                            other.execute(text('UPDATE fixture_b SET version=2'))
                        writes.append(True)
                event.listen(engine.sync_engine,'before_cursor_execute',update_between_reads)
                with patch.object(exporter,'async_session_maker',sessions), \
                     patch.object(exporter,'get_settings',return_value=SimpleNamespace(mysql_database=engine.url.database)), \
                     patch.object(exporter,'ensure_backup_root',return_value=root), \
                     patch('common.utils.backup_paths.get_backup_root',return_value=root):
                    await exporter.DbBackupTaskService().execute()
                    event.remove(engine.sync_engine,'before_cursor_execute',update_between_reads)
                    self.assertEqual(writes,[True])
                    async with sessions() as session,restore_fixture() as restored:
                        log=await session.scalar(select(DbBackupLog))
                        self.assertEqual(log.status,'success')
                        result=await BackupVerificationService(session).verify(log.id,7,restored)
                        self.assertEqual(result['status'],'restored',result)
                        async with restored.connect() as conn:
                            versions=[(await conn.execute(text(f'SELECT version FROM {table}'))).scalar_one()
                                      for table in ('fixture_a','fixture_b')]
                        self.assertEqual(versions,[1,1])
                    async with engine.connect() as conn:
                        self.assertEqual((await conn.execute(text('SELECT version FROM fixture_b'))).scalar_one(),2)
            finally:
                writer.dispose();await engine.dispose()

    async def test_nontransactional_table_is_not_reported_as_consistent_backup(self):
        module_path=Path(__file__).resolve().parents[2]/'scheduler/app/services/scheduler/db_backup_task.py'
        spec=importlib.util.spec_from_file_location('backup_engine_roundtrip',module_path)
        exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)
        with tempfile.TemporaryDirectory(prefix='xymb-backup-engine-') as temp,commerce_fixture() as url:
            engine=create_async_engine(url,hide_parameters=True)
            sessions=async_sessionmaker(engine,expire_on_commit=False);root=Path(temp)
            try:
                async with engine.begin() as conn:
                    for model in (DbBackupLog,BackupVerification,AdminAudit,AdminAuditArchive):await conn.run_sync(model.__table__.create)
                    await conn.execute(text('CREATE TABLE fixture_nontransactional (id INT PRIMARY KEY) ENGINE=MyISAM'))
                    await conn.execute(text('INSERT INTO fixture_nontransactional VALUES(1)'))
                with patch.object(exporter,'async_session_maker',sessions), \
                     patch.object(exporter,'get_settings',return_value=SimpleNamespace(mysql_database=engine.url.database)), \
                     patch.object(exporter,'ensure_backup_root',return_value=root), \
                     patch('common.utils.backup_paths.get_backup_root',return_value=root):
                    await exporter.DbBackupTaskService().execute()
                async with sessions() as session:
                    log=await session.scalar(select(DbBackupLog))
                    self.assertEqual(log.status,'failed')
                    self.assertEqual(log.error_message,'NonTransactionalBackupTable')
                    self.assertIsNone(log.file_name)
                self.assertEqual(list(root.glob('*.sql.gz')),[])
            finally:
                await engine.dispose()
