"""账号执行入口：Redis 租期 + 持久代次/配置/凭据条件，网络故障闭合。"""
from __future__ import annotations

import asyncio
import time
from sqlalchemy import select
from common.models.xy_account import XYAccount
from common.services import account_policy as policy
from common.services.account_execution import AccountLease, ExecutionLost


class AccountRuntime:
    def __init__(self, account_id, owner_id, redis, session_factory=None):
        if session_factory is None:
            from common.db.session import async_session_maker
            session_factory = async_session_maker
        self.account_id, self.owner_id = account_id, owner_id
        self.sessions = session_factory
        self.lease = AccountLease(redis, account_id)
        self.version = None
        self.generation = None
        self.renew_task = None

    async def _account(self, db, *, lock=False):
        stmt = select(XYAccount).where(XYAccount.account_id == self.account_id,
                                      XYAccount.owner_id == self.owner_id)
        if lock:
            stmt = stmt.with_for_update()
        a = (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()
        if not a:
            raise ExecutionLost('账号不存在或身份已变更')
        return a

    def _validate(self, a):
        state = policy.snapshot(a)
        version = (state['credential_version'], state['config_version'])
        if a.status != 'active' or state['business_state'] in {
            'paused', 'verification_required', 'proxy_error', 'cooldown', 'recovering',
        }:
            raise ExecutionLost('账号业务已暂停')
        if self.version is not None and (self.version != version or self.generation != state['generation']):
            raise ExecutionLost('账号版本或执行代次已变更')
        return state

    async def start(self):
        if not await self.lease.acquire():
            return False
        try:
            async with self.sessions() as db:
                a = await self._account(db, lock=True)
                state = self._validate(a)
                self.generation = max(state['generation'] + 1, self.lease.generation)
                self.version = (state['credential_version'], state['config_version'])
                state.update(generation=self.generation, connection_state='connecting',
                             business_state='unchecked', reason='awaiting_registration')
                state['consumers']['websocket'] = state['config_version']
                policy.store(a, state)
                await self.lease.check()
                await db.commit()
            self.renew_task = asyncio.create_task(self._renew())
            return True
        except BaseException:
            await self.lease.release()
            raise

    async def _renew(self):
        while True:
            await asyncio.sleep(self.lease.ttl_ms / 3000)
            if not await self.lease.renew():
                return

    async def check(self):
        await self.lease.check()
        try:
            async with self.sessions() as db:
                self._validate(await self._account(db))
        except ExecutionLost:
            self.lease.lost = True
            raise
        except Exception as exc:
            self.lease.lost = True
            raise ExecutionLost('账号状态核验失败') from exc

    async def check_business(self):
        from common.services.account_request_budget import BudgetError
        await self.check()
        async with self.sessions() as session:
            account = await self._account(session)
            state = self._validate(account)
            if state['business_state'] != 'ready' or state.get('connection_state') != 'connected':
                raise BudgetError('account_not_ready')
            return account

    async def before_business_request(self):
        from common.services.account_request_budget import AccountRequestBudget, RequestBudgetPolicy, account_risk_config
        account = await self.check_business()
        policy = RequestBudgetPolicy.from_risk_config(account_risk_config(account))
        await AccountRequestBudget(self.lease.redis).acquire(self.account_id, policy, check=self.check_business)

    async def load_binding(self):
        """Load the credentials and proxy belonging to this exact acquired version."""
        await self.lease.check()
        async with self.sessions() as session:
            account = await self._account(session)
            self._validate(account)
            binding = {'cookie': account.cookie, 'unb': account.unb, 'owner_id': account.owner_id,
                       'proxy': {name: getattr(account, name) for name in
                           ('proxy_type', 'proxy_host', 'proxy_port', 'proxy_user', 'proxy_pass')}}
            await self.lease.check()
            return binding

    async def record_connection(self, connected, *, verified=False):
        await self.lease.check()
        async with self.sessions() as db:
            a = await self._account(db, lock=True)
            state = self._validate(a)
            state['connection_state'] = 'connected' if connected else 'disconnected'
            if connected and verified:
                state.update(business_state='ready', reason=None, last_success_at=time.time())
            elif not connected and state['business_state'] == 'ready':
                state.update(business_state='unchecked', reason='connection_disconnected')
            policy.store(a, state)
            await self.lease.check()
            await db.commit()

    async def confirm_registration(self, message, expected_mid):
        """A connected socket is not business readiness; require its own positive /reg receipt."""
        if not expected_mid or not isinstance(message, dict):
            return False
        headers = message.get('headers')
        body = message.get('body')
        if (not isinstance(headers, dict) or headers.get('mid') != expected_mid
                or message.get('code') not in (200, '200')
                or (body is not None and not isinstance(body, dict))
                or (isinstance(body, dict) and (body.get('reason')
                    or body.get('code') not in (None, 0, '0', 200, '200')))):
            return False
        await self.record_connection(True, verified=True)
        return True

    async def pause(self, reason):
        await self.lease.check()
        async with self.sessions() as db:
            account = await self._account(db, lock=True)
            state = self._validate(account)
            state.update(business_state='proxy_error' if reason == 'proxy' else 'paused',
                         reason=reason, connection_state='disconnected')
            policy.store(account, state)
            await self.lease.check()
            await db.commit()

    async def close(self):
        if self.renew_task:
            self.renew_task.cancel()
            await asyncio.gather(self.renew_task, return_exceptions=True)
        # 仅当前代次更新断连，旧实例退出不清理接任状态。
        try:
            await self.record_connection(False)
        except Exception:
            pass
        await self.lease.release()
