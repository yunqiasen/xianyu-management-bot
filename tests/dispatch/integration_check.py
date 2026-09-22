"""显式运行：隔离 MySQL 19006 / Redis 19379；平台边界替身，不启动任何账号连接。"""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4

from dotenv import dotenv_values
env_path = os.environ.get('XYMB_INTEGRATION_ENV') or os.environ.get('DISPATCH_INTEGRATION_ENV')
if not env_path:
    raise RuntimeError('XYMB_INTEGRATION_ENV required')
ENV = dotenv_values(env_path)
assert ENV['MYSQL_HOST'] in ('127.0.0.1', 'localhost') and ENV['MYSQL_PORT'] == '19006'
assert ENV['REDIS_HOST'] in ('127.0.0.1', 'localhost') and ENV['REDIS_PORT'] == '19379'
for key, value in ENV.items():
    if value is not None: os.environ[key] = value
ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'websocket'))

from types import SimpleNamespace
import httpx
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import URL, delete, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from common.models.account_operation import AccountOperation, upgrade_account_operations
from common.models.xy_account import XYAccount
from common.services.account_dispatch import AccountDispatchClient, DispatchRequest, OperationStore
from common.services.account_request_budget import AccountRequestBudget, RequestBudgetPolicy, BudgetError
from common.services.account_runtime import AccountRuntime
from common.services.account_execution import FencedSocket
from app.services.account_dispatcher import AccountDispatcher
from app.api.routes.account_operations import router, get_account_dispatcher
from app.core.config import get_settings


class MySQLRedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(URL.create('mysql+asyncmy', username=ENV['MYSQL_USER'],
            password=ENV['MYSQL_PASSWORD'], host='127.0.0.1', port=19006, database=ENV['MYSQL_DATABASE']), echo=False)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.redis = Redis(host='127.0.0.1', port=19379, db=int(ENV.get('REDIS_DB', '0')),
                           password=ENV.get('REDIS_PASSWORD') or None)
        await self.redis.ping()
        async with self.engine.begin() as conn:
            await conn.run_sync(upgrade_account_operations)
            await conn.run_sync(upgrade_account_operations)
        self.account_id = 'dispatch-' + uuid4().hex
        async with self.sessions() as db:
            account = XYAccount(owner_id=910007, account_id=self.account_id, unb='101',
                cookie='unb=101; _m_h5_tk=offline_fixture;', login_method='manual', status='active',
                metadata_json={'xy_runtime': {'config_values': {'risk': {'min_interval_seconds': .001}}}})
            db.add(account); await db.commit()
        self.runtime = AccountRuntime(self.account_id, 910007, self.redis, self.sessions)
        self.assertTrue(await self.runtime.start())
        await self.runtime.record_connection(True, verified=True)
        # 使用真实 XianyuAsync 属性与 mid 接收分派。跳过构造/登录/网络启动。
        from app.services.xianyu.xianyu_async import XianyuAsync
        self.live = object.__new__(XianyuAsync)
        self.live._account_runtime = self.runtime
        self.live._pending_mid_futures = {}; self.live.myid = '101'; self.live.cookie_id = self.account_id
        self.packets = []; self.reply = True
        fixture = self
        class Wire:
            async def send(self, text):
                packet = json.loads(text); fixture.packets.append(packet)
                if fixture.reply:
                    fixture.live._dispatch_mid_response({'headers': packet['headers'], 'code': 200,
                                                          'body': {'messageId': 'fixture-mid'}})
        self.live.connection_manager = SimpleNamespace(ws=FencedSocket(Wire(), self.runtime.check))
        self.budget = AccountRequestBudget(self.redis)
        self.manager = SimpleNamespace(instances={self.account_id: self.live})
        self.dispatcher = AccountDispatcher(self.manager, self.sessions, self.budget, response_timeout=.05)

    async def asyncTearDown(self):
        if hasattr(self, 'runtime'):
            await self.runtime.close()
            await self.redis.delete(self.runtime.lease.key, self.runtime.lease.generation_key,
                                    self.budget.key(self.account_id))
        if hasattr(self, 'account_id'):
            async with self.sessions() as db:
                await db.execute(delete(AccountOperation).where(AccountOperation.account_id == self.account_id,
                                                                 AccountOperation.owner_id == 910007))
                await db.execute(delete(XYAccount).where(XYAccount.account_id == self.account_id, XYAccount.owner_id == 910007))
                await db.commit()
        if hasattr(self, 'redis'): await self.redis.aclose()
        if hasattr(self, 'engine'): await self.engine.dispose()

    def request(self, request_id='r1'):
        return DispatchRequest(owner_id=910007, account_id=self.account_id, request_id=request_id,
            generation=self.runtime.generation, config_version=0, credential_version=0, command='send_text_message',
            payload={'cid': 'fixture-chat', 'to_user_id': 'fixture-buyer', 'text': 'offline fixture'})

    async def test_sql_unique_claim_concurrency_and_recreated_dispatcher(self):
        results = await asyncio.gather(*(self.dispatcher.execute(self.request()) for _ in range(10)))
        self.assertTrue(all(r.status in {'confirmed', 'submitted'} for r in results), [(r.status, r.error_code) for r in results])
        self.assertEqual(len(self.packets), 1)
        recreated = AccountDispatcher(self.manager, self.sessions, self.budget)
        result = await recreated.execute(self.request())
        self.assertEqual(result.status, 'confirmed')
        self.assertEqual(len(self.packets), 1)
        async with self.sessions() as db:
            rows = (await db.execute(select(AccountOperation).where(AccountOperation.account_id == self.account_id))).scalars().all()
            self.assertEqual(len(rows), 1)

    async def test_real_lua_atomic_budget_and_persistent_retry_after(self):
        policy = RequestBudgetPolicy(1000)
        async def take():
            try: await self.budget.take(self.account_id, policy); return True
            except BudgetError: return False
        allowed = await asyncio.gather(*(take() for _ in range(20)))
        self.assertEqual(sum(allowed), 1)
        await self.budget.defer(self.account_id, 60)
        second = AccountRequestBudget(self.redis)
        await second.defer(self.account_id, 1)
        with self.assertRaises(BudgetError) as caught: await second.take(self.account_id, policy)
        self.assertGreater(caught.exception.retry_after, 58)

    async def test_actual_receive_loop_method_and_internal_http_client(self):
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[get_account_dispatcher] = lambda: self.dispatcher
        settings = get_settings(); old = settings.internal_api_token
        settings.internal_api_token = 'dispatch-fixture-' + 'x' * 40
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as http:
                client = AccountDispatchClient('http://fixture', settings.internal_api_token, http=http)
                result = await client.execute(self.request())
                self.assertEqual(result.status, 'confirmed')
                self.assertEqual(result.result['messageId'], 'fixture-mid')
                self.assertEqual((await client.get_operation(910007, self.account_id, 'r1')).status, 'confirmed')
        finally: settings.internal_api_token = old

    async def test_timeout_then_new_runtime_does_not_resend_unknown(self):
        self.reply = False
        self.assertEqual((await self.dispatcher.execute(self.request())).status, 'unknown')
        await self.runtime.close()
        self.runtime = AccountRuntime(self.account_id, 910007, self.redis, self.sessions)
        self.assertTrue(await self.runtime.start())
        self.live._account_runtime = self.runtime
        self.assertEqual((await self.dispatcher.execute(self.request())).status, 'unknown')
        self.assertEqual(len(self.packets), 1)


if __name__ == '__main__': unittest.main(verbosity=2)
