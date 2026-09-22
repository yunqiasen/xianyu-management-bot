"""S1/S2/S3: start the three real bootstraps against a newly created, disposable schema."""
from pathlib import Path
import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.verification.run import commerce_fixture, source_fingerprint, validate_integration_environment


class ThreeServiceSmoke(unittest.TestCase):
    def test_current_source_boots_all_consumers_and_applies_exact_configuration(self):
        import httpx
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url
        output=Path(os.environ['XYMB_VERIFY_OUTPUT']).resolve();output.mkdir(parents=True,exist_ok=True)
        private=Path(os.environ['XYMB_INTEGRATION_ENV'])
        validate_integration_environment(private)
        from dotenv import dotenv_values
        env={**os.environ,**{k:v for k,v in dotenv_values(private).items() if v is not None}}
        env.pop('XYMB_BUILD_COMMIT',None)
        sockets=[socket.socket() for _ in range(3)]
        for item in sockets:item.bind(('127.0.0.1',0))
        ports=[item.getsockname()[1] for item in sockets]
        for item in sockets:item.close()
        processes=[]; logs=[]; before=source_fingerprint(ROOT)
        with commerce_fixture() as database_url:
            url=make_url(database_url)
            env.update(MYSQL_HOST=url.host,MYSQL_PORT=str(url.port),MYSQL_USER=url.username,
                       MYSQL_PASSWORD=url.password,MYSQL_DATABASE=url.database,
                       PYTHONPATH=str(ROOT),HOST='127.0.0.1',LOG_LEVEL='INFO',SQL_ECHO='false',
                       DB_POOL_SIZE='2',DB_MAX_OVERFLOW='0',DB_POOL_TIMEOUT='5',
                       AUTO_START_WEBSOCKET='false',AUTO_START_SCHEDULER='false',AUTO_START_CRAWL_JOBS='false',
                       ENABLE_REMOTE_ADS='false',ENABLE_REMOTE_ANNOUNCEMENTS='false',ENABLE_REMOTE_POPUP_ANNOUNCEMENTS='false',
                       STATIC_DIR=str(output/'static'),BACKUP_DIR=str(output/'backups'),ADMIN_RESTORE_DATABASE_URL='',
                       BACKEND_WEB_PORT=str(ports[0]),WEBSOCKET_PORT=str(ports[1]),SCHEDULER_PORT=str(ports[2]),
                       BACKEND_WEB_SERVICE_URL=f'http://127.0.0.1:{ports[0]}',
                       WEBSOCKET_SERVICE_URL=f'http://127.0.0.1:{ports[1]}',SCHEDULER_SERVICE_URL=f'http://127.0.0.1:{ports[2]}')
            engine=create_engine(url.set(drivername='mysql+pymysql'),hide_parameters=True)
            health=[]
            try:
                with httpx.Client(trust_env=False,timeout=4) as client:
                    for name,port in zip(('backend-web','websocket','scheduler'),ports):
                        log=(output/(name+'.log')).open('w'); logs.append(log)
                        process=subprocess.Popen([str(ROOT/'.venv/bin/python'),'main.py'],cwd=ROOT/name,
                            env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                        processes.append(process)
                        deadline=time.monotonic()+100
                        payload=None
                        while time.monotonic()<deadline:
                            self.assertIsNone(process.poll(),f'{name}_startup_exit')
                            try:
                                response=client.get(f'http://127.0.0.1:{port}/health')
                                if response.status_code==200:
                                    payload=response.json();break
                            except httpx.HTTPError:pass
                            time.sleep(.2)
                        self.assertIsNotNone(payload,f'{name}_health_timeout')
                        self.assertTrue(payload['success'])
                        self.assertEqual(payload['data']['database'],'connected')
                        self.assertEqual(payload['data']['redis'],'connected')
                        self.assertFalse(payload['data']['workers_enabled'])
                        health.append(payload['data'])
                    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
                    self.assertEqual({item['commit'] for item in health},{head})
                    self.assertEqual(len({item['version'] for item in health}),1)
                    with engine.begin() as connection:
                        token=connection.execute(text("SELECT value FROM xy_system_settings WHERE `key`='security.internal_api_token'")).scalar_one()
                        state={'xy_runtime':{'config_version':1,'credential_version':0,'generation':0,
                            'business_state':'disabled','consumers':{'web':1},'config_values':{'risk':{'min_interval_seconds':5}}}}
                        connection.execute(text("INSERT INTO xy_accounts (owner_id,account_id,unb,cookie,login_method,status,metadata) VALUES (7,'smoke-account','887701','unb=887701','manual','disabled',:metadata)"),{'metadata':json.dumps(state)})
                    for port in ports[1:]:
                        target=f'http://127.0.0.1:{port}/internal/account-configuration'
                        self.assertEqual(client.post(target,json={}).status_code,401)
                        headers={'X-Internal-Token':token}
                        invalid=client.post(target,headers=headers,json={'owner_id':'SECRET_INPUT'})
                        self.assertEqual(invalid.status_code,422)
                        self.assertNotIn('SECRET_INPUT',invalid.text)
                        request={'owner_id':7,'account_id':'smoke-account','config_version':1}
                        applied=client.post(target,headers=headers,json=request)
                        self.assertEqual(applied.status_code,200,applied.text)
                        self.assertTrue(applied.json()['applied'],applied.text)
                        self.assertEqual(client.post(target,headers=headers,json={**request,'config_version':0}).status_code,409)
                    with engine.connect() as connection:
                        raw=connection.execute(text("SELECT metadata,status FROM xy_accounts WHERE account_id='smoke-account'")).one()
                        metadata=json.loads(raw[0]) if isinstance(raw[0],str) else raw[0]
                        self.assertEqual(metadata['xy_runtime']['consumers'],{'web':1,'websocket':1,'scheduler':1})
                        self.assertEqual(raw[1],'disabled')
                    (output/'health.json').write_text(json.dumps({'services':health,'synthetic_accounts':True,'production_touched':False},indent=2)+'\n')
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        os.killpg(process.pid,signal.SIGTERM)
                        try:process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
                for log in logs:log.close()
                engine.dispose()
        self.assertEqual(source_fingerprint(ROOT),before)


if __name__=='__main__':unittest.main()
