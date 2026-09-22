"""S3 continuation: synthetic source -> actual model rows and business readers."""
import asyncio
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.mysql import LONGTEXT
from fixtures import KEY, make_source, add_extended_source
from tools.migration.mapping import plan, model_tables
from tools.migration.snapshot import Snapshot, MigrationError, digest
from tools.migration.runner import Migrator, migration_metadata

@compiles(LONGTEXT, 'sqlite')
def longtext(element, compiler, **kw): return 'TEXT'

class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = add_extended_source(make_source(self.root / 'source.sqlite'))
        self.engine = create_engine('sqlite:///' + str(self.root / 'target.sqlite'))
        self.addCleanup(self.engine.dispose)
        self.runner = Migrator(self.engine, key=KEY, archive_dir=self.root/'archive')

    def prepare(self, **kw): return plan(Snapshot.read(self.path), namespace='fixture', key=KEY, **kw)
    def apply(self, prepared):
        for name in dict.fromkeys(s.target for s in prepared.steps):
            model_tables()[name].create(self.engine, checkfirst=True)
        migration_metadata.create_all(self.engine)
        self.runner.apply(prepared, quarantine=True)
    def rows(self, name):
        with self.engine.connect() as c: return [dict(r) for r in c.execute(select(model_tables()[name])).mappings()]

    def test_encrypted_source_key_explicit_and_wrong_key_rejected_before_writes(self):
        source_key = Fernet.generate_key()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE cookies SET value=? WHERE id='account-a'", ('enc$'+Fernet(source_key).encrypt(b'SYNTHETIC_COOKIE').decode(),))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(MigrationError, 'legacy_secret_authentication_failed'):
            self.prepare(source_key=Fernet.generate_key())
        prepared = self.prepare(source_key=source_key); self.apply(prepared)
        self.assertEqual(next(r for r in self.rows('xy_accounts') if r['account_id']=='account-a')['cookie'], 'SYNTHETIC_COOKIE')
        self.assertNotIn('SYNTHETIC_COOKIE', json.dumps(prepared.report()))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.runner.read_archive(prepared.checksum)['tables']['cookies'][0]['value'].startswith('enc$'))

    def test_key_file_private_and_environment_explicit(self):
        from tools.migration.legacy import load_source_key
        key = Fernet.generate_key(); f = self.root/'key'; f.write_bytes(key); f.chmod(0o600)
        self.assertEqual(load_source_key(key_file=f), key)
        f.chmod(0o644)
        with self.assertRaisesRegex(MigrationError, 'source_key_file_not_private'): load_source_key(key_file=f)
        with patch.dict(os.environ, {'FIXTURE_SOURCE_KEY':key.decode()}):
            self.assertEqual(load_source_key(env='FIXTURE_SOURCE_KEY'), key)
            self.assertIsNone(load_source_key())

    def test_per_key_settings_conversion_zero_boolean_and_unknown_kept_out(self):
        with sqlite3.connect(self.path) as db:
            db.executemany('INSERT INTO system_settings VALUES(?,?)', [('registration_enabled','0'),('smtp_port','0'),('theme_color','blue')])
            db.execute("INSERT INTO user_settings VALUES(2,1,'theme_color','blue')")
        prepared = self.prepare(); self.apply(prepared)
        settings = {r['key']:r['value'] for r in self.rows('xy_system_settings')}
        self.assertEqual(settings, {'registration_enabled':'false','smtp_port':'0','theme.color_preset':'ocean'})
        self.assertEqual(self.rows('xy_user_settings')[0]['key'], 'theme.color_preset')
        self.assertTrue(any(i['code']=='setting_key_review_required' for i in prepared.issues))

    def test_pause_snapshot_bound_to_source_owner_age_and_actual_reader(self):
        now = time.time(); snap = Snapshot.read(self.path)
        state = {'namespace':'fixture','source_checksum':snap.checksum(KEY),'captured_at':now,
                 'accounts':[{'account_id':'account-a','user_id':1,'paused_chats':{'chat-shared@goofish':now+600}}]}
        prepared = self.prepare(runtime_state=state, now=now); self.apply(prepared)
        async def check():
            from common.services.reply_state import ReplyState
            engine = create_async_engine('sqlite+aiosqlite:///'+str(self.root/'target.sqlite'))
            try:
                service=ReplyState(async_sessionmaker(engine))
                self.assertAlmostEqual(await service.pause_remaining('account-a','chat-shared',now=now),600)
                self.assertEqual(await service.pause_remaining('account-b','chat-shared',now=now),0)
            finally: await engine.dispose()
        asyncio.run(check())
        self.assertEqual(self.runner.apply(prepared,quarantine=True)['inserted'],0)
        for changed in ({'captured_at':now-901}, {'namespace':'wrong'}, {'source_checksum':'0'*64}):
            with self.assertRaises(MigrationError): self.prepare(runtime_state={**state,**changed}, now=now)
        state['accounts'][0]['user_id']=2
        with self.assertRaisesRegex(MigrationError,'runtime_owner_mismatch'): self.prepare(runtime_state=state,now=now)
        self.assertFalse(any(s.target=='xy_reply_pauses' for s in self.prepare().steps))

    def test_simple_old_template_renders_through_existing_service(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE notification_templates SET template='{account_id}: {message} / {time}'")
        prepared=self.prepare(); self.apply(prepared)
        async def check():
            from common.services.notification_template_service import NotificationTemplateService
            engine=create_async_engine('sqlite+aiosqlite:///'+str(self.root/'target.sqlite'))
            try:
                async with async_sessionmaker(engine)() as session:
                    owner=self.rows('xy_notify_templates')[0]['owner_id']
                    text, failed=await NotificationTemplateService(session).render_event(owner,'message',{'account_id':'account-a','summary':'hello','time':'now'})
                    self.assertFalse(failed); self.assertEqual(text,'account-a: hello / now')
            finally: await engine.dispose()
        asyncio.run(check())

    def test_owner_scoped_attachment_relocation_updates_rule_and_business_reader(self):
        from PIL import Image
        import hashlib
        buf=io.BytesIO(); Image.new('RGB',(2,2),'red').save(buf,format='PNG'); data=buf.getvalue()
        source_assets=self.root/'legacy-assets'; source_assets.mkdir(); (source_assets/'fixture.png').write_bytes(data)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE keywords SET type='image',image_url='fixture.png'")
        prepared=self.prepare()
        manifest=[{'reference':'fixture.png','source_owner':1,'sha256':hashlib.sha256(data).hexdigest()}]
        moved=self.runner.relocate_attachments(prepared,manifest,source_root=source_assets,target_root=self.root/'uploads')
        self.apply(moved)
        image=self.rows('xy_reply_images')[0]; rule=self.rows('xy_keyword_rules')[0]
        self.assertEqual(rule['image_url'], image['url'])
        self.assertEqual(image['owner_id'],rule['owner_id'])
        self.assertEqual(len(self.rows('xy_reply_image_refs')),1)
        self.assertFalse(any(i['table']=='keywords' and i['code']=='attachment_verification_required' for i in moved.issues))
        async def check():
            from common.services.reply_images import ReplyImages
            engine=create_async_engine('sqlite+aiosqlite:///'+str(self.root/'target.sqlite'))
            try:
                service=ReplyImages(async_sessionmaker(engine),root=self.root/'uploads')
                self.assertEqual(await service.read(image['owner_id'],image['id']),data)
                with self.assertRaises(ValueError): await service.read(image['owner_id']+1,image['id'])
                with self.assertRaises(ValueError): await service.delete(image['owner_id'],image['id'])
            finally: await engine.dispose()
        asyncio.run(check())
        self.assertEqual(self.runner.apply(moved,quarantine=True)['inserted'],0)
        for change, code in [({'sha256':'0'*64},'attachment_hash_mismatch'),({'source_owner':2},'attachment_owner_mismatch')]:
            with self.assertRaisesRegex(MigrationError,code):
                self.runner.relocate_attachments(self.prepare(),[{**manifest[0],**change}],source_root=source_assets,target_root=self.root/'other')
        self.assertFalse((self.root/'other').exists())

    def test_rules_specs_rating_logs_project_real_models_without_historical_invention(self):
        with sqlite3.connect(self.path) as db:
            db.executescript('''
            ALTER TABLE cards ADD COLUMN spec_name_2 TEXT;
            ALTER TABLE cards ADD COLUMN spec_value_2 TEXT;
            UPDATE cards SET is_multi_spec=1,spec_name='颜色',spec_value='红',spec_name_2='尺码',spec_value_2='大';
            ALTER TABLE orders ADD COLUMN spec_name TEXT;
            ALTER TABLE orders ADD COLUMN spec_value TEXT;
            ALTER TABLE orders ADD COLUMN spec_name_2 TEXT;
            ALTER TABLE orders ADD COLUMN spec_value_2 TEXT;
            UPDATE orders SET spec_name='颜色',spec_value='红',spec_name_2='尺码',spec_value_2='大';
            CREATE TABLE delivery_rules(id INTEGER PRIMARY KEY,user_id INTEGER,keyword TEXT,card_id INTEGER,delivery_count INTEGER,enabled INTEGER,delivery_times INTEGER);
            INSERT INTO delivery_rules VALUES(1,1,'fixture',1,1,1,9);
            CREATE TABLE comment_templates(id INTEGER PRIMARY KEY,cookie_id TEXT,name TEXT,content TEXT,is_active INTEGER,sort_order INTEGER);
            INSERT INTO comment_templates VALUES(1,'account-a','fixture','谢谢',1,0);
            CREATE TABLE scheduled_rate_logs(id INTEGER PRIMARY KEY,batch_id TEXT,cookie_id TEXT,order_id TEXT,status TEXT,comment TEXT,raw_response TEXT);
            INSERT INTO scheduled_rate_logs VALUES(1,'batch','account-a','order-a','success','谢谢','{"secret":"FAKE"}');
            CREATE TABLE scheduled_red_flower_logs(id INTEGER PRIMARY KEY,batch_id TEXT,cookie_id TEXT,order_id TEXT,status TEXT,raw_response TEXT);
            INSERT INTO scheduled_red_flower_logs VALUES(1,'batch','account-a','order-a','processing','{}');
            CREATE TABLE scheduled_tasks(id INTEGER PRIMARY KEY,name TEXT,task_type TEXT,account_id TEXT,enabled INTEGER,interval_hours INTEGER,user_id INTEGER);
            INSERT INTO scheduled_tasks VALUES(1,'求花','auto_red_flower','account-a',1,24,1);
            ''')
        prepared=self.prepare(); self.apply(prepared)
        card=self.rows('xy_cards')[0]; order=self.rows('xy_orders')[0]
        self.assertEqual(card['spec_name'],order['spec_name']); self.assertIn('尺码',card['spec_name'])
        self.assertEqual(self.rows('xy_delivery_rules')[0]['keyword'],'fixture')
        self.assertEqual(self.rows('xy_delivery_rules')[0]['delivery_times'],9)
        config=self.rows('xy_auto_rate_configs')[0]
        self.assertEqual(config['text_content'],'谢谢'); self.assertFalse(config['enabled'])
        self.assertEqual(self.rows('xy_scheduled_rate_log')[0]['status'],'success')
        self.assertEqual(self.rows('xy_scheduled_red_flower_log')[0]['status'],'unknown')
        self.assertEqual(self.rows('xy_delivery_intents')[0]['content_state'],'unknown')
        self.assertFalse(order['is_rated']) # no promotion of a log to a platform order fact
        self.assertTrue(any(i['code']=='unsupported_schedule_type' for i in prepared.issues))
        self.assertEqual(self.runner.apply(prepared,quarantine=True)['inserted'],0)

    def test_rating_template_library_and_feedback_attempts_are_native_and_no_retry(self):
        with sqlite3.connect(self.path) as db:
            db.executescript('''
            CREATE TABLE comment_templates(id INTEGER PRIMARY KEY,cookie_id TEXT,name TEXT,content TEXT,is_active INTEGER,sort_order INTEGER);
            INSERT INTO comment_templates VALUES(1,'account-a','active','谢谢',1,0);
            INSERT INTO comment_templates VALUES(2,'account-a','spare','欢迎',0,1);
            CREATE TABLE scheduled_red_flower_logs(id INTEGER PRIMARY KEY,batch_id TEXT,cookie_id TEXT,order_id TEXT,status TEXT,created_at TEXT);
            INSERT INTO scheduled_red_flower_logs VALUES(1,'batch','account-a','order-a','processing','2026-09-01 00:00:00');
            ''')
        prepared=self.prepare()
        self.assertTrue(any(s.target=='xy_product_rate_templates' for s in prepared.steps))
        self.assertTrue(any(s.target=='xy_product_feedback_attempts' for s in prepared.steps))
        self.apply(prepared)
        templates=self.rows('xy_product_rate_templates')
        self.assertEqual(len(templates),2)
        self.assertEqual([r['content'] for r in templates if r['active']],['谢谢'])
        attempt=self.rows('xy_product_feedback_attempts')[0]
        self.assertEqual(attempt['status'],'unknown')
        self.assertEqual(attempt['kind'],'red_flower')
        self.assertIsNone(attempt['retry_at'])
        async def check():
            from common.services.product_feedback_service import ProductFeedbackService
            engine=create_async_engine('sqlite+aiosqlite:///'+str(self.root/'target.sqlite'))
            try:
                async with async_sessionmaker(engine)() as session:
                    rows=await ProductFeedbackService(session).history(attempt['owner_id'],'account-a')
                    self.assertEqual(rows[0]['status'],'unknown')
            finally: await engine.dispose()
        asyncio.run(check())

    def test_pause_capture_from_legacy_tuple_keys_and_runtime_archive(self):
        from tools.migration.legacy import capture_runtime_state
        now=time.time(); snap=Snapshot.read(self.path)
        state=capture_runtime_state({('account-a','chat'):now+500},snap,namespace='fixture',key=KEY,now=now)
        prepared=self.prepare(runtime_state=state,now=now)
        self.apply(prepared)
        artifacts=list((self.root/'archive').glob('runtime-*.enc'))
        self.assertEqual(len(artifacts),1)
        self.assertEqual(self.runner.read_bundle(artifacts[0].name),state)
        with self.assertRaisesRegex(MigrationError,'runtime_owner_mismatch'):
            capture_runtime_state({('missing','chat'):now+500},snap,namespace='fixture',key=KEY,now=now)

    def test_attachment_source_and_destination_symlinks_rejected(self):
        from PIL import Image
        import hashlib
        data=io.BytesIO(); Image.new('RGB',(1,1),'red').save(data,format='PNG'); content=data.getvalue()
        assets=self.root/'assets'; assets.mkdir(); (self.root/'outside.png').write_bytes(content)
        (assets/'link.png').symlink_to(self.root/'outside.png')
        with sqlite3.connect(self.path) as db: db.execute("UPDATE keywords SET image_url='link.png'")
        manifest=[{'reference':'link.png','source_owner':1,'sha256':hashlib.sha256(content).hexdigest()}]
        with self.assertRaisesRegex(MigrationError,'attachment_read_error'):
            self.runner.relocate_attachments(self.prepare(),manifest,source_root=assets,target_root=self.root/'uploads')

    def test_invalid_boolean_and_template_collision_stay_explicit(self):
        with sqlite3.connect(self.path) as db: db.execute("INSERT INTO system_settings VALUES('registration_enabled','maybe')")
        with self.assertRaisesRegex(MigrationError,'setting_boolean_invalid'): self.prepare()
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM system_settings WHERE key='registration_enabled'")
            db.execute("INSERT INTO notification_templates VALUES(2,'token_refresh','{error_message}')")
            db.execute("INSERT INTO notification_templates VALUES(3,'account_paused','{error_message}')")
        prepared=self.prepare()
        self.assertFalse(any(i['code']=='notification_event_collision' for i in prepared.issues))
        self.assertEqual({s.values['event_type'] for s in prepared.steps if s.target=='xy_notify_templates'}, {'message','token_refresh','account_paused'})
        self.assertFalse(any(s.target=='xy_notify_templates' and s.values['event_type']=='account_error' for s in prepared.steps))

    def test_attachment_original_url_local_path_and_chat_identity(self):
        from PIL import Image
        import hashlib
        content=io.BytesIO(); Image.new('RGB',(2,2),'blue').save(content,format='PNG'); data=content.getvalue()
        (self.root/'photo.png').write_bytes(data)
        ref='/static/uploads/images/old.png'
        with sqlite3.connect(self.path) as db:
            db.executescript('ALTER TABLE chat_messages ADD COLUMN content_type INTEGER; ALTER TABLE chat_messages ADD COLUMN image_url TEXT;')
            db.execute('UPDATE chat_messages SET content_type=2,image_url=?,content=?',(ref,'[图片]'))
        prepared=self.prepare()
        moved=self.runner.relocate_attachments(prepared,[{'reference':ref,'local_path':'photo.png','source_owner':1,'sha256':hashlib.sha256(data).hexdigest()}],source_root=self.root,target_root=self.root/'uploads')
        self.apply(moved)
        image=self.rows('xy_reply_images')[0]
        events=[r for r in self.rows('xy_reply_events') if r['content_type']=='image']
        self.assertEqual(events[0]['content'],image['url'])
        self.assertEqual(events[0]['account_id'],'account-a')
        self.assertEqual(self.rows('xy_reply_image_refs')[0]['owner_id'],image['owner_id'])
        self.assertFalse(any(i['code']=='rich_message_attachment_requires_review' for i in moved.issues))
