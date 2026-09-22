"""S3: real MySQL persistence must retain subsecond deadlines and timestamps."""
import os
from pathlib import Path
import unittest
from uuid import uuid4
from sqlalchemy import create_engine, MetaData, Table, Column, Integer, select, insert
from sqlalchemy.engine import URL

@unittest.skipUnless(os.environ.get('XYMB_INTEGRATION_ENV'), 'explicit isolated infrastructure required')
class TimestampPrecisionTests(unittest.TestCase):
    def test_deadlines_survive_mysql_roundtrip(self):
        from common.models.bargaining_event import BargainingEvent
        from common.models.reply_state import reply_pauses, reply_events
        from common.models.admin_control import LoginProtection, DataPreview
        from common.models.notification_delivery import NotificationEvent, NotificationDelivery
        from common.models.listing_monitor_reliability import ListingMonitorState
        fields = [BargainingEvent.__table__.c.occurred_at, reply_pauses.c.until, reply_events.c.occurred_at,
                  LoginProtection.__table__.c.locked_until, DataPreview.__table__.c.expires_at,
                  NotificationEvent.__table__.c.first_seen, NotificationEvent.__table__.c.last_seen,
                  NotificationDelivery.__table__.c.due_at, ListingMonitorState.__table__.c.lease_until]
        values = dict(line.split('=',1) for line in Path(os.environ['XYMB_INTEGRATION_ENV']).read_text().splitlines() if '=' in line and not line.startswith('#'))
        self.assertEqual((values['MYSQL_HOST'],values['MYSQL_PORT'],values['MYSQL_DATABASE']), ('127.0.0.1','19006','xymb_integration'))
        engine = create_engine(URL.create('mysql+pymysql', username=values['MYSQL_USER'],password=values['MYSQL_PASSWORD'],host='127.0.0.1',port=19006,database='xymb_integration'),hide_parameters=True)
        metadata = MetaData()
        table = Table('fixture_clock_' + uuid4().hex[:16], metadata, Column('id',Integer,primary_key=True),
                      *(Column('t'+str(i), field.type.copy()) for i,field in enumerate(fields)))
        try:
            with engine.begin() as conn:
                metadata.create_all(conn)
                conn.execute(insert(table).values(id=1, **{'t'+str(i):1790073219.125 for i in range(len(fields))}))
                row=conn.execute(select(table)).mappings().one()
                for i,field in enumerate(fields):
                    with self.subTest(column=str(field)):
                        self.assertAlmostEqual(row['t'+str(i)],1790073219.125,places=3)
        finally:
            with engine.begin() as conn: metadata.drop_all(conn)
            engine.dispose()

    def test_startup_widens_existing_columns_without_losing_rows(self):
        import secrets,subprocess
        from sqlalchemy import inspect,text
        from common.db.fork_schema import upgrade_fork_schema
        from sqlalchemy.dialects.mysql import DOUBLE
        container='xymb-integration-mysql-1'
        label=subprocess.check_output(['docker','inspect','--format','{{ index .Config.Labels "com.docker.compose.project" }}',container],text=True).strip()
        self.assertEqual(label,'xymb-integration')
        suffix=secrets.token_hex(8);schema='xymb_precision_'+suffix;user='clock_'+suffix;password=secrets.token_hex(24)
        def admin(sql):
            proc=subprocess.run(['docker','exec','-i',container,'sh','-c','MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=socket -uroot --batch'],input=sql,text=True,capture_output=True)
            self.assertEqual(proc.returncode,0,'isolated fixture setup failed')
        engine=None
        try:
            admin(f"CREATE DATABASE `{schema}`; CREATE USER '{user}'@'%' IDENTIFIED BY '{password}'; GRANT ALL ON `{schema}`.* TO '{user}'@'%';")
            engine=create_engine(URL.create('mysql+pymysql',username=user,password=password,host='127.0.0.1',port=19006,database=schema),hide_parameters=True)
            with engine.begin() as conn:
                upgrade_fork_schema(conn)
                conn.execute(text('ALTER TABLE xy_reply_pauses MODIFY COLUMN until FLOAT NOT NULL'))
                conn.execute(text("INSERT INTO xy_reply_pauses(id,account_id,chat_id,until,version) VALUES ('fixture','a','c',128,1)"))
                upgrade_fork_schema(conn);upgrade_fork_schema(conn)
                field=next(c for c in inspect(conn).get_columns('xy_reply_pauses') if c['name']=='until')
                self.assertIsInstance(field['type'],DOUBLE)
                self.assertEqual(conn.execute(text("SELECT until FROM xy_reply_pauses WHERE id='fixture'")).scalar(),128)
                conn.execute(text("UPDATE xy_reply_pauses SET until=1790073219.125 WHERE id='fixture'"))
                self.assertAlmostEqual(conn.execute(text("SELECT until FROM xy_reply_pauses WHERE id='fixture'")).scalar(),1790073219.125,places=3)
        finally:
            if engine:engine.dispose()
            admin(f"DROP DATABASE IF EXISTS `{schema}`; DROP USER IF EXISTS '{user}'@'%';")
