"""Real isolated MySQL S3; creates and drops only a random migration fixture schema.

Never loads application .env. Root authentication stays inside the explicitly
label-checked integration container; its value is never read or emitted here.
"""
import os
import secrets
import sqlite3
import time
import io
import hashlib
from cryptography.fernet import Fernet
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, select, update, func
from sqlalchemy.engine import URL
from fixtures import KEY, make_source, add_extended_source
from tools.migration.snapshot import Snapshot, MigrationError
from tools.migration.mapping import plan, model_tables
from tools.migration.runner import Migrator, migration_metadata


class MySQLTests(unittest.TestCase):
    def test_real_mysql_import_resume_repeat_verify_and_delta_preservation(self):
        container = 'xymb-integration-mysql-1'
        if not shutil.which('docker'):
            self.skipTest('isolated integration Docker not available')
        check = subprocess.run(['docker', 'inspect', '--format', '{{ index .Config.Labels "com.docker.compose.project" }}', container], capture_output=True, text=True)
        if check.returncode or check.stdout.strip() != 'xymb-integration':
            self.skipTest('isolated integration MySQL container not available')
        suffix = secrets.token_hex(8)
        schema, user, password = 'xymb_migration_' + suffix, 'mig_' + suffix, secrets.token_hex(24)
        def sql(statement):
            run = subprocess.run(['docker', 'exec', '-i', container, 'sh', '-c',
                'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=socket -uroot --batch'],
                input=statement, capture_output=True, text=True, timeout=30)
            if run.returncode:
                self.fail('isolated fixture MySQL administration failed; credentials suppressed')
        engine = None
        try:
            sql(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin; CREATE USER '{user}'@'%' IDENTIFIED BY '{password}'; GRANT ALL ON `{schema}`.* TO '{user}'@'%';")
            engine = create_engine(URL.create('mysql+pymysql', username=user, password=password,
                host='127.0.0.1', port=19006, database=schema), hide_parameters=True, echo=False)
            with tempfile.TemporaryDirectory() as tmp:
                path = add_extended_source(make_source(Path(tmp) / 'synthetic.sqlite'))
                source_key=Fernet.generate_key()
                with sqlite3.connect(path) as c:
                    c.execute("UPDATE cookies SET value=? WHERE id='account-a'",('enc$'+Fernet(source_key).encrypt(b'FAKE_MYSQL_COOKIE').decode(),))
                    c.execute("INSERT INTO system_settings VALUES('registration_enabled','0')")
                    c.execute("UPDATE keywords SET type='image',image_url='fixture.png'")
                    c.executescript("CREATE TABLE comment_templates(id INTEGER PRIMARY KEY,cookie_id TEXT,name TEXT,content TEXT,is_active INTEGER,sort_order INTEGER); INSERT INTO comment_templates VALUES(1,'account-a','fixture','谢谢',1,0);")
                    c.executescript("CREATE TABLE scheduled_red_flower_logs(id INTEGER PRIMARY KEY,batch_id TEXT,cookie_id TEXT,order_id TEXT,status TEXT); INSERT INTO scheduled_red_flower_logs VALUES(1,'batch','account-a','order-a','processing');")
                original = path.read_bytes()
                snapshot=Snapshot.read(path); now=time.time()
                runtime={'namespace':'fixture','source_checksum':snapshot.checksum(KEY),'captured_at':now,
                         'accounts':[{'account_id':'account-a','user_id':1,'paused_chats':{'fixture-pause':now+600}}]}
                prepared = plan(snapshot, namespace='fixture', key=KEY,source_key=source_key,runtime_state=runtime,now=now)
                runner = Migrator(engine, key=KEY, archive_dir=Path(tmp) / 'batch')
                from PIL import Image
                asset=io.BytesIO(); Image.new('RGB',(3,3),'blue').save(asset,format='PNG'); image=asset.getvalue()
                (Path(tmp)/'fixture.png').write_bytes(image)
                prepared=runner.relocate_attachments(prepared,[{'reference':'fixture.png','source_owner':1,'sha256':hashlib.sha256(image).hexdigest()}],source_root=tmp,target_root=Path(tmp)/'uploads')
                tables = model_tables()
                for name in dict.fromkeys(s.target for s in prepared.steps):
                    tables[name].create(engine, checkfirst=True)
                migration_metadata.create_all(engine)
                runner = Migrator(engine, key=KEY, archive_dir=Path(tmp) / 'batch')
                with self.assertRaisesRegex(MigrationError, 'injected_interrupt'):
                    runner.apply(prepared, quarantine=True, interrupt_after=4)
                runner.apply(prepared, quarantine=True)
                repeated = runner.apply(prepared, quarantine=True)
                self.assertEqual(repeated['unchanged'], len(prepared.steps))
                verified = runner.verify(prepared, account_id='account-a')
                self.assertTrue(verified['verified'])
                self.assertTrue(verified['account_disabled'])
                with engine.begin() as c:
                    self.assertEqual(c.scalar(select(func.count()).select_from(tables['xy_accounts'])), 2)
                    self.assertEqual(c.scalar(select(tables['xy_accounts'].c.cookie).where(tables['xy_accounts'].c.account_id=='account-a')),'FAKE_MYSQL_COOKIE')
                    self.assertEqual(c.scalar(select(tables['xy_system_settings'].c.value)),'false')
                    self.assertEqual(c.scalar(select(tables['xy_product_rate_templates'].c.content)),'谢谢')
                    self.assertEqual(c.scalar(select(tables['xy_product_feedback_attempts'].c.status)),'unknown')
                    self.assertGreater(c.scalar(select(tables['xy_reply_pauses'].c.until)),now)
                    image_row=c.execute(select(tables['xy_reply_images'])).mappings().one()
                    self.assertEqual(c.scalar(select(tables['xy_keyword_rules'].c.image_url)),image_row['url'])
                    self.assertEqual(c.scalar(select(func.count()).select_from(tables['xy_reply_image_refs'])),1)
                    order = dict(c.execute(select(tables['xy_orders'])).mappings().one())
                    self.assertEqual(order['quantity'], 2)
                    material = c.execute(select(tables['xy_product_materials'])).mappings().one()
                    self.assertEqual(material['sku_rows'][0]['stock'], 0)
                    self.assertEqual(c.scalar(select(tables['xy_publish_logs'].c.status)), 'unknown')
                    self.assertEqual(c.scalar(select(tables['xy_ai_presets'].c.name)), 'fixture preset')
                    self.assertEqual(str(order['amount']), '0.00')
                    c.execute(update(tables['xy_orders']).values(status='refunding'))
                with self.assertRaisesRegex(MigrationError, 'target_changed'):
                    runner.apply(prepared, quarantine=True, incremental=True)
                export = runner.export_rollback(prepared, account_id='account-a')
                bundle = runner.read_bundle(export['bundle'])
                self.assertEqual(bundle['tables']['xy_orders'][0]['status'], 'refunding')
                self.assertEqual(bundle['tables']['xy_delivery_intents'][0]['reserved_lines'], ['FAKE_RESERVED_CARD'])
                self.assertEqual(original, path.read_bytes())
                from tools.migration.rollback import RollbackImporter
                stage=Path(tmp)/'rollback.sqlite'
                with sqlite3.connect(path) as src, sqlite3.connect(stage) as dst: src.backup(dst)
                importer=RollbackImporter(runner,stage,account_id='account-a',namespace='fixture')
                consumed=importer.apply(export['bundle'],execute=True)
                self.assertEqual(consumed['conflicts'],[])
                self.assertEqual(importer.apply(export['bundle'],execute=True)['written'],0)
                with sqlite3.connect(stage) as c:
                    self.assertEqual(c.execute("SELECT order_status FROM orders WHERE order_id='order-a'").fetchone()[0],'refunding')
        finally:
            if engine is not None:
                engine.dispose()
            sql(f"DROP DATABASE IF EXISTS `{schema}`; DROP USER IF EXISTS '{user}'@'%';")
