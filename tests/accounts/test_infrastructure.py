"""真实隔离 MySQL/Redis：只新增/删除本次唯一账号与 Redis 键。"""
import asyncio
import os
from pathlib import Path
import unittest
import uuid
from sqlalchemy import select,delete
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
import redis.asyncio as redis
from common.models.xy_account import XYAccount
from common.services.account_runtime import AccountRuntime
from common.services.account_execution import ExecutionLost
from common.services.account_recovery import AccountRecovery
from common.services import account_policy as p

@unittest.skipUnless(os.getenv('XYMB_INTEGRATION_ENV'),'设置隔离测试环境文件后执行')
class InfrastructureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        vals=dict(line.split('=',1) for line in Path(os.environ['XYMB_INTEGRATION_ENV']).read_text().splitlines() if '=' in line and not line.startswith('#'))
        if vals.get('MYSQL_HOST')!='127.0.0.1' or vals.get('MYSQL_PORT')!='19006' or vals.get('MYSQL_DATABASE')!='xymb_integration':
            raise RuntimeError('仅使用既定隔离数据库')
        self.engine=create_async_engine(URL.create('mysql+asyncmy',username=vals['MYSQL_USER'],password=vals['MYSQL_PASSWORD'],host='127.0.0.1',port=19006,database='xymb_integration'))
        self.sessions=async_sessionmaker(self.engine,expire_on_commit=False)
        self.redis=redis.Redis(host='127.0.0.1',port=19379,decode_responses=True)
        self.aid='accounts-test-'+uuid.uuid4().hex;self.owner=910000000+uuid.uuid4().int%9000000
        self.workers=[]
        async with self.sessions() as db:
            db.add(XYAccount(owner_id=self.owner,account_id=self.aid,cookie=f'unb={self.owner}; token=fixture',unb=str(self.owner),status='active',login_method='manual',metadata_json={'existing':False}))
            await db.commit()

    async def asyncTearDown(self):
        for runtime in self.workers: await runtime.close()
        async with self.sessions() as db:
            await db.execute(delete(XYAccount).where(XYAccount.account_id==self.aid,XYAccount.owner_id==self.owner));await db.commit()
        if self.workers: await self.redis.delete(self.workers[0].lease.key,self.workers[0].lease.generation_key)
        await self.redis.aclose();await self.engine.dispose()

    def worker(self,owner=None):
        w=AccountRuntime(self.aid,owner or self.owner,self.redis,self.sessions);self.workers.append(w);return w

    async def test_mysql_redis_runtime_competition_takeover_fences_old(self):
        first,second=self.worker(),self.worker()
        claims=await asyncio.gather(first.start(),second.start())
        self.assertEqual(sum(claims),1)
        holder=first if claims[0] else second
        await holder.record_connection(True,verified=True)
        await self.redis.delete(holder.lease.key)
        successor=self.worker();self.assertTrue(await successor.start())
        self.assertGreater(successor.generation,holder.generation)
        with self.assertRaises(ExecutionLost): await holder.check()
        await holder.close(); await successor.check()
        async with self.sessions() as db:
            a=(await db.execute(select(XYAccount).where(XYAccount.account_id==self.aid))).scalar_one()
            self.assertFalse(a.metadata_json['existing']);self.assertEqual(p.snapshot(a)['generation'],successor.generation)

    async def test_mysql_row_lock_coalesces_jobs_and_exactly_one_inner_attempt(self):
        services=[AccountRecovery(self.aid,self.owner,self.sessions) for _ in range(5)]
        jobs=await asyncio.gather(*(s.start('invalid_credentials') for s in services))
        self.assertEqual(len({j['id'] for j in jobs}),1)
        results=await asyncio.gather(*(s.take(jobs[0]['id'],'renew') for s in services),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,dict) for x in results),1)
        async with self.sessions() as db:
            a=(await db.execute(select(XYAccount).where(XYAccount.account_id==self.aid))).scalar_one()
            self.assertEqual(p.snapshot(a)['recovery_attempts'],1)

    async def test_mysql_owner_and_version_change_block_runtime(self):
        wrong=self.worker(self.owner+1)
        with self.assertRaises(ExecutionLost): await wrong.start()
        worker=self.worker();self.assertTrue(await worker.start())
        async with self.sessions() as db:
            a=(await db.execute(select(XYAccount).where(XYAccount.account_id==self.aid).with_for_update())).scalar_one()
            p.bump_config(a,{'false':False,'zero':0,'null':None});await db.commit()
        with self.assertRaises(ExecutionLost): await worker.check()

    async def test_mysql_same_platform_identity_has_one_owner_under_concurrent_creation(self):
        from sqlalchemy import inspect
        from common.db.fork_schema import upgrade_account_identity
        from common.models.user import User
        from app.services.account_service import AccountService
        suffix=uuid.uuid4().hex
        ids=[self.aid+'-alias1', self.aid+'-alias2']
        owners=[self.owner+20000, self.owner+20001]
        identity='identity-'+suffix
        async with self.engine.begin() as connection:
            await connection.run_sync(upgrade_account_identity)
            indexes=await connection.run_sync(lambda c: inspect(c).get_indexes('xy_accounts'))
        self.assertTrue(any(index.get('unique') and index['column_names']==['unb'] for index in indexes))
        async with self.sessions() as db:
            db.add_all([User(id=owner,username=f'identity-{suffix}-{n}',email=f'{suffix}-{n}@example.test',password_hash='fixture')
                        for n,owner in enumerate(owners)])
            await db.commit()
        try:
            ready=asyncio.Event()
            async def create(owner,aid):
                await ready.wait()
                async with self.sessions() as db:
                    try:
                        row,_,_,_=await AccountService(db).stage_cookie_import(owner,aid,
                            f'unb={identity}; token=fixture',create_only=True)
                        return row.owner_id
                    except ValueError:
                        await db.rollback()
                        return None
            tasks=[asyncio.create_task(create(owner,aid)) for owner,aid in zip(owners,ids)]
            ready.set()
            outcomes=await asyncio.gather(*tasks)
            self.assertEqual(sum(outcome is not None for outcome in outcomes),1)
            async with self.sessions() as db:
                rows=(await db.execute(select(XYAccount).where(XYAccount.unb==identity))).scalars().all()
                self.assertEqual(len(rows),1)
                self.assertIn(rows[0].owner_id,outcomes)
                self.assertEqual(rows[0].cookie,'')  # credential verification is still pending
        finally:
            async with self.sessions() as db:
                await db.execute(delete(XYAccount).where(XYAccount.account_id.in_(ids),XYAccount.owner_id.in_(owners)))
                await db.execute(delete(User).where(User.id.in_(owners)))
                await db.commit()

    async def test_mysql_parallel_consumer_acknowledgments_preserve_each_service_version(self):
        from common.services.account_configuration import ConfigurationStore
        async with self.sessions() as db:
            account=await db.scalar(select(XYAccount).where(XYAccount.account_id==self.aid).with_for_update())
            p.bump_config(account)
            version=p.snapshot(account)['config_version']
            await db.commit()
        store=ConfigurationStore(self.sessions)
        results=await asyncio.gather(*(store.acknowledge(self.owner,self.aid,version,consumer)
            for consumer in ('web','websocket','scheduler')))
        self.assertTrue(all(result['applied'] for result in results))
        account=await store.read(self.owner,self.aid,version)
        self.assertEqual(p.pending_consumers(account),[])
        self.assertNotEqual(p.snapshot(account)['business_state'],'ready')
