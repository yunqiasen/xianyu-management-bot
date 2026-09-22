import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from sqlalchemy import create_engine, select, insert, update
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.mysql import LONGTEXT
from fixtures import KEY, make_source
from tools.migration.mapping import plan, model_tables
from tools.migration.runner import Migrator, migration_metadata
from tools.migration.snapshot import Snapshot, MigrationError

@compiles(LONGTEXT,'sqlite')
def compile_longtext(element,compiler,**kw): return 'TEXT'

class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_stop_dryrun_never_calls_and_execute_requires_observed_boundary(self):
        from tools.migration.control import LegacyControl
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a): pass
            def do_PUT(self):
                calls.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"enabled":false}')
            def do_GET(self):
                calls.append((self.path,None))
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({'cookie_id':'fixture-a','runtime_status':{'running':False,'instance_exists':False}}).encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            endpoint=f'http://127.0.0.1:{server.server_port}'
            dry=LegacyControl(endpoint,'fixture-a',token='SYNTHETIC',execute=False)
            self.assertFalse((await dry.quiesce())['stopped']); self.assertEqual(calls,[])
            adapter=LegacyControl(endpoint,'fixture-a',token='SYNTHETIC',execute=True)
            result=await adapter.quiesce()
            self.assertFalse(result['stopped']) # runtime absence alone proves no durable inflight boundary
            async def boundary(): return {'account_id':'fixture-a','captured_at':time.time(),'stopped':True,'watermark':'fixture-offset','inflight':[]}
            adapter=LegacyControl(endpoint,'fixture-a',token='SYNTHETIC',execute=True,boundary_reader=boundary)
            self.assertTrue((await adapter.quiesce())['stopped'])
            self.assertEqual(calls[0],('/cookies/fixture-a/status',{'enabled':False}))
            async def wrong(): return {'account_id':'other','captured_at':time.time(),'stopped':True,'watermark':1,'inflight':[]}
            adapter.boundary_reader=wrong
            with self.assertRaisesRegex(MigrationError,'legacy_boundary_invalid'): await adapter.quiesce()
        finally: await asyncio.to_thread(server.shutdown); server.server_close(); thread.join()

class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.source=make_source(self.root/'baseline.sqlite')
        self.engine=create_engine('sqlite:///'+str(self.root/'target.sqlite')); self.addCleanup(self.engine.dispose)
        self.prepared=plan(Snapshot.read(self.source),namespace='fixture',key=KEY)
        for target in dict.fromkeys(s.target for s in self.prepared.steps): model_tables()[target].create(self.engine,checkfirst=True)
        migration_metadata.create_all(self.engine)
        self.runner=Migrator(self.engine,key=KEY,archive_dir=self.root/'archive'); self.runner.apply(self.prepared,quarantine=True)
        self.stage=self.root/'rollback.sqlite'
        with sqlite3.connect(self.source) as src, sqlite3.connect(self.stage) as dst: src.backup(dst)

    def test_export_consume_repeat_and_three_way_conflicts_preserve_new_facts(self):
        from tools.migration.rollback import RollbackImporter
        t=model_tables()
        with self.engine.begin() as c:
            account=c.execute(select(t['xy_accounts']).where(t['xy_accounts'].c.account_id=='account-a')).mappings().one()
            c.execute(update(t['xy_orders']).values(status='refunding'))
            c.execute(insert(t['xy_orders']).values(id=998,owner_id=account['owner_id'],account_id='account-a',order_no='new-order',quantity=1,amount=9,status='pending_verification',source='fixture'))
            c.execute(insert(t['xy_reply_events']).values(account_id='account-a',chat_id='new-chat',event_id='new-event',message_id='new-event',role='user',origin='platform',sender_id='buyer',content='new message',occurred_at=time.time()))
            c.execute(update(t['xy_cards']).where(t['xy_cards'].c.user_id==account['owner_id']).values(data_content='NEW_FREE_POOL'))
        exported=self.runner.export_rollback(self.prepared,account_id='account-a')
        before=self.source.read_bytes()
        importer=RollbackImporter(self.runner,self.stage,account_id='account-a',namespace='fixture')
        dry=importer.apply(exported['bundle'],execute=False)
        self.assertGreater(dry['planned'],0)
        report=importer.apply(exported['bundle'],execute=True)
        self.assertEqual(report['conflicts'],[])
        with sqlite3.connect(self.stage) as c:
            self.assertEqual(c.execute("SELECT order_status FROM orders WHERE order_id='order-a'").fetchone()[0],'refunding')
            self.assertEqual(c.execute("SELECT order_status FROM orders WHERE order_id='new-order'").fetchone()[0],'unknown')
            self.assertEqual(c.execute("SELECT COUNT(*) FROM chat_messages WHERE content='new message'").fetchone()[0],1)
            self.assertEqual(c.execute("SELECT data_content FROM cards WHERE id=1").fetchone()[0],'NEW_FREE_POOL')
            self.assertEqual(c.execute("SELECT enabled FROM cookie_status WHERE cookie_id='account-a'").fetchone()[0],0)
        self.assertEqual(importer.apply(exported['bundle'],execute=True)['written'],0)
        self.assertEqual(before,self.source.read_bytes())
        with sqlite3.connect(self.stage) as c: c.execute("UPDATE orders SET order_status='completed' WHERE order_id='order-a'")
        conflict=importer.apply(exported['bundle'],execute=True)
        self.assertTrue(conflict['conflicts'])
        self.assertNotIn('new message',json.dumps(conflict))
        with sqlite3.connect(self.stage) as c:
            self.assertEqual(c.execute("SELECT order_status FROM orders WHERE order_id='order-a'").fetchone()[0],'completed')
        self.assertTrue(report['reconciliation_required']) # unresolved aggregate intent not invented as per-unit fact

    def test_rollback_wrong_account_and_owner_and_corrupt_bundle_rejected(self):
        from tools.migration.rollback import RollbackImporter
        exported=self.runner.export_rollback(self.prepared,account_id='account-a')
        importer=RollbackImporter(self.runner,self.stage,account_id='account-b',namespace='fixture')
        with self.assertRaisesRegex(MigrationError,'rollback_source_mismatch'): importer.apply(exported['bundle'],execute=True)
        payload=self.runner.read_bundle(exported['bundle']); payload['tables']['xy_orders'][0]['account_id']='account-b'
        altered=self.runner.write_bundle('rollback-altered.enc',payload)
        importer=RollbackImporter(self.runner,self.stage,account_id='account-a',namespace='fixture')
        with self.assertRaisesRegex(MigrationError,'rollback_account_mismatch'): importer.apply(altered,execute=True)

    def test_new_reservations_consumed_without_fabricating_sent_unit_history(self):
        from tools.migration.rollback import RollbackImporter
        t=model_tables()
        with self.engine.begin() as c:
            a=c.execute(select(t['xy_accounts']).where(t['xy_accounts'].c.account_id=='account-a')).mappings().one()
            card=c.execute(select(t['xy_cards'])).mappings().one()
            c.execute(insert(t['xy_orders']).values(id=999,owner_id=a['owner_id'],account_id='account-a',order_no='reserve-new',quantity=1,amount=0,status='pending',source='fixture'))
            c.execute(insert(t['xy_delivery_intents']).values(id='fixture-new-intent',owner_id=a['owner_id'],account_id='account-a',order_no='reserve-new',operation_key='payment',card_id=card['id'],card_type='data',quantity=1,mode='send_first',content_state='unknown',confirm_state='unknown',reserved_lines=['NEW_RESERVED'],evidence=[]))
        exported=self.runner.export_rollback(self.prepared,account_id='account-a')
        importer=RollbackImporter(self.runner,self.stage,account_id='account-a',namespace='fixture')
        importer.apply(exported['bundle'],execute=True)
        with sqlite3.connect(self.stage) as c:
            row=c.execute("SELECT card_id,unit_index,reserved_content,status FROM data_card_reservations WHERE order_id='reserve-new'").fetchone()
            self.assertEqual(row,(1,1,'NEW_RESERVED','reserved'))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM delivery_finalization_states WHERE order_id='reserve-new'").fetchone()[0],0)
        self.assertEqual(importer.apply(exported['bundle'],execute=True)['written'],0)

    def test_stage_source_change_blocks_all_fact_writes(self):
        from tools.migration.rollback import RollbackImporter
        t=model_tables()
        with self.engine.begin() as c: c.execute(update(t['xy_orders']).values(status='refunding'))
        with sqlite3.connect(self.stage) as c: c.execute("UPDATE orders SET order_status='completed'")
        exported=self.runner.export_rollback(self.prepared,account_id='account-a')
        report=RollbackImporter(self.runner,self.stage,account_id='account-a',namespace='fixture').apply(exported['bundle'],execute=True)
        self.assertEqual(report['written'],0); self.assertEqual(len(report['conflicts']),1)
        with sqlite3.connect(self.stage) as c:
            self.assertEqual(c.execute('SELECT enabled FROM cards').fetchone()[0],1)
            self.assertEqual(c.execute("SELECT enabled FROM cookie_status WHERE cookie_id='account-a'").fetchone()[0],0)

    def test_export_baseline_contains_only_selected_account_and_owner(self):
        exported=self.runner.export_rollback(self.prepared,account_id='account-a')
        baseline=self.runner.read_bundle(exported['bundle'])['source_tables']
        self.assertEqual([r['id'] for r in baseline['cookies']],['account-a'])
        self.assertEqual([r['id'] for r in baseline['users']],[1])
        self.assertNotIn('FAKE_COOKIE_B',json.dumps(baseline))

    def test_rollback_three_way_merges_independent_old_remark_not_overwrite(self):
        from tools.migration.rollback import RollbackImporter
        with sqlite3.connect(self.stage) as c:
            c.execute("UPDATE cookies SET remark='old-local-note' WHERE id='account-a'")
        exported=self.runner.export_rollback(self.prepared,account_id='account-a')
        report=RollbackImporter(self.runner,self.stage,account_id='account-a',namespace='fixture').apply(exported['bundle'],execute=True)
        self.assertEqual(report['conflicts'],[])
        with sqlite3.connect(self.stage) as c:
            self.assertEqual(c.execute("SELECT remark FROM cookies WHERE id='account-a'").fetchone()[0],'old-local-note')

    def test_successive_delta_consumption_uses_prior_receipt_and_rejects_stale_package(self):
        from tools.migration.rollback import RollbackImporter
        t=model_tables()
        with self.engine.begin() as c: c.execute(update(t['xy_orders']).values(status='shipped'))
        first=self.runner.export_rollback(self.prepared,account_id='account-a')
        importer=RollbackImporter(self.runner,self.stage,account_id='account-a',namespace='fixture')
        self.assertEqual(importer.apply(first['bundle'],execute=True)['conflicts'],[])
        with self.engine.begin() as c: c.execute(update(t['xy_orders']).values(status='refunding'))
        second=self.runner.export_rollback(self.prepared,account_id='account-a')
        self.assertEqual(importer.apply(second['bundle'],execute=True)['conflicts'],[])
        with sqlite3.connect(self.stage) as c:
            self.assertEqual(c.execute('SELECT order_status FROM orders').fetchone()[0],'refunding')
        with self.assertRaisesRegex(MigrationError,'rollback_stale_package'): importer.apply(first['bundle'],execute=True)

    def test_export_retains_shared_global_rules_and_default_templates_without_other_tenants(self):
        t=model_tables()
        for name in ('xy_delivery_rules','xy_reply_filters','xy_notify_templates'):
            t[name].create(self.engine,checkfirst=True)
        with self.engine.begin() as c:
            account=c.execute(select(t['xy_accounts']).where(t['xy_accounts'].c.account_id=='account-a')).mappings().one()
            owner=account['owner_id']
            c.execute(insert(t['xy_delivery_rules']).values(id='own-rule',owner_id=owner,card_id=1,keyword='global',account_id=None))
            c.execute(insert(t['xy_delivery_rules']).values(id='other-rule',owner_id=owner+1,card_id=1,keyword='OTHER_SECRET',account_id=None))
            c.execute(insert(t['xy_reply_filters']).values(id='own-filter',owner_id=owner,account_id='',pattern='shared',match_mode='contains',source='user',actions=['skip_reply']))
            c.execute(insert(t['xy_reply_filters']).values(id='other-filter',owner_id=owner+1,account_id='',pattern='OTHER_SECRET',match_mode='contains',source='user',actions=['skip_reply']))
            c.execute(insert(t['xy_notify_templates']).values(owner_id=0,event_type='message',body='default {summary}'))
        result=self.runner.export_rollback(self.prepared,account_id='account-a')
        payload=self.runner.read_bundle(result['bundle'])
        self.assertEqual([r['id'] for r in payload['tables']['xy_delivery_rules']],['own-rule'])
        self.assertEqual([r['id'] for r in payload['tables']['xy_reply_filters']],['own-filter'])
        self.assertEqual(payload['tables']['xy_notify_templates'][0]['owner_id'],0)
        self.assertNotIn('OTHER_SECRET',json.dumps(payload))
        from tools.migration.rollback import RollbackImporter
        report=RollbackImporter(self.runner,self.stage,account_id='account-a',namespace='fixture').apply(result['bundle'])
        self.assertTrue(report['reconciliation_required'])

    def test_multiple_fulfillment_lines_are_retained_without_inventing_legacy_unit_indexes(self):
        from tools.migration.rollback import RollbackImporter
        t=model_tables()
        with self.engine.begin() as c:
            account=c.execute(select(t['xy_accounts']).where(t['xy_accounts'].c.account_id=='account-a')).mappings().one()
            card=c.execute(select(t['xy_cards'])).mappings().one()
            c.execute(insert(t['xy_orders']).values(id=991,owner_id=account['owner_id'],account_id='account-a',order_no='multi-new',quantity=2,status='pending_verification',source='fixture'))
            for index,line in enumerate(('red','blue')):
                c.execute(insert(t['xy_delivery_intents']).values(id='multi-'+line,owner_id=account['owner_id'],account_id='account-a',order_no='multi-new',line_id=line,operation_key='payment:'+line,card_id=card['id'],card_type='data',quantity=1,mode='send_first',content_state='unknown',confirm_state='unknown',reserved_lines=['HELD-'+line],evidence=[]))
        exported=self.runner.export_rollback(self.prepared,account_id='account-a')
        result=RollbackImporter(self.runner,self.stage,account_id='account-a',namespace='fixture').apply(exported['bundle'],execute=True)
        self.assertIn('multiline_reservation_reconciliation_required',[gap['code'] for gap in result['gaps']])
        with sqlite3.connect(self.stage) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM data_card_reservations WHERE order_id='multi-new'").fetchone()[0],0)
        payload=self.runner.read_bundle(exported['bundle'])
        self.assertEqual(len([r for r in payload['tables']['xy_delivery_intents'] if r['order_no']=='multi-new']),2)

class RunningControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_runtime_blocks_even_if_boundary_file_claims_stopped(self):
        from tools.migration.control import LegacyControl
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a): pass
            def do_PUT(self):
                self.rfile.read(int(self.headers['Content-Length'])); self.send_response(200); self.end_headers(); self.wfile.write(b'{"enabled":false}')
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"cookie_id":"fixture-a","runtime_status":{"running":true,"instance_exists":true}}')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            async def boundary(): return {'account_id':'fixture-a','captured_at':time.time(),'stopped':True,'watermark':1,'inflight':[]}
            control=LegacyControl(f'http://127.0.0.1:{server.server_port}','fixture-a',token='FAKE',execute=True,boundary_reader=boundary)
            result=await control.quiesce()
            self.assertFalse(result['stopped'])
            self.assertEqual(result['reason'],'legacy_runtime_still_running')
        finally:
            await asyncio.to_thread(server.shutdown); server.server_close(); thread.join()
