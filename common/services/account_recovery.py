"""持久恢复任务。每个真正的续期/登录尝试先扣同一账号预算；不另抢执行权。"""
import random
import time
from sqlalchemy import select
from common.models.xy_account import XYAccount
from common.services import account_policy as p


class AccountRecovery:
    def __init__(self, account_id, owner_id, sessions=None):
        if sessions is None:
            from common.db.session import async_session_maker
            sessions = async_session_maker
        self.account_id, self.owner_id, self.sessions = account_id, owner_id, sessions

    async def account(self, db):
        a = (await db.execute(select(XYAccount).where(XYAccount.account_id == self.account_id,
             XYAccount.owner_id == self.owner_id).with_for_update())).scalar_one_or_none()
        if not a: raise p.StaleAccountOperation('账号身份已变化')
        return a

    async def start(self, reason, *, expected=None):
        async with self.sessions() as db:
            a = await self.account(db); state = p.snapshot(a)
            if expected is not None and tuple(state[k] for k in ('credential_version','config_version','generation')) != tuple(expected):
                raise p.StaleAccountOperation('恢复来源版本已过期')
            if a.status != 'active': return None
            if reason not in {'invalid_credentials','renewal'}:
                p.begin_recovery(a, reason); await db.commit(); return None
            now = time.time()
            for job_id in list(state['jobs']):
                p.get_job(a, job_id, owner_id=self.owner_id, now=now)
            state = p.snapshot(a)
            for job in state['jobs'].values():
                if job['status'] not in p.TERMINAL:
                    await db.commit()
                    return job if job.get('automatic') else None
            if (now < state['next_retry_at'] or state['recovery_attempts'] >= 2 or
                    state['business_state'] in {'paused','proxy_error','verification_required'}):
                await db.commit()
                return None
            if reason == 'invalid_credentials': p.record_invalidation(a)
            if p.snapshot(a)['business_state'] == 'paused':
                await db.commit(); return None
            job = p.start_job(a, owner_id=self.owner_id, kind='password_login')
            state = p.snapshot(a); job.update(automatic=True, steps=[])
            state['jobs'][job['id']] = job; state['recovery_running'] = True
            p.store(a,state); await db.commit(); return job

    def validate(self, a, job_id):
        job = p.get_job(a, job_id, owner_id=self.owner_id)
        state = p.snapshot(a)
        if a.status != 'active' or job['status'] in p.TERMINAL or any(state[k] != job[k] for k in ('credential_version','config_version','generation')):
            raise p.StaleAccountOperation('恢复任务已结束或版本已变化')
        return state, job

    async def take(self, job_id, step):
        async with self.sessions() as db:
            a = await self.account(db); state, job = self.validate(a,job_id)
            if state['recovery_attempts'] >= 2 or time.time() < state['next_retry_at']:
                raise p.StaleAccountOperation('恢复预算已耗尽或仍在冷却')
            state['recovery_attempts'] += 1
            state['next_retry_at'] = time.time() + random.uniform(48,72)
            state['jobs'][job_id]['steps'].append({'step':step,'at':time.time()})
            p.store(a,state); await db.commit()
            return job

    async def complete(self, job_id, cookie):
        async with self.sessions() as db:
            a=await self.account(db)
            accepted=p.complete_job(a,job_id,cookie)
            if accepted:
                state=p.snapshot(a); state.update(recovery_running=False,recovery_attempts=0,next_retry_at=0)
                p.store(a,state)
            await db.commit(); return accepted

    async def stop(self, job_id, reason):
        async with self.sessions() as db:
            a=await self.account(db)
            try: state, job=self.validate(a,job_id)
            except p.StaleAccountOperation: return
            state['jobs'][job_id].update(status='invalid',reason=reason)
            business={'proxy':'proxy_error','verification':'verification_required','rate_limit':'cooldown'}.get(reason,'paused')
            if reason=='network' and state['recovery_attempts']<2: business='recovering'
            if reason=='rate_limit':
                state['rate_limit_count']=state.get('rate_limit_count',0)+1
                state['next_retry_at']=time.time()+(60 if state['rate_limit_count']==1 else 300)
                if state['rate_limit_count']>=3: business='paused'
            state.update(recovery_running=False,reason=reason,business_state=business)
            p.store(a,state); await db.commit()

    async def run(self, job_id, *, renew, login, sleep=None):
        import asyncio
        from common.services.account_credentials import CredentialRejected
        sleep = sleep or asyncio.sleep
        try:
            async with self.sessions() as db:
                a=await self.account(db); state, job=self.validate(a,job_id)
                steps=job.get('steps',[])
                next_step = 'password_login' if job.get('password_allowed') else 'renew'
                if steps and next_step == 'renew':
                    # 前次进程在请求后中断，结果未知，禁止擅自升级为密码重登。
                    return False
            if next_step == 'renew':
                await self.take(job_id,'renew')
                try:
                    cookie=await asyncio.wait_for(renew(), max(.01,job['expires_at']-time.time()))
                    return await self.complete(job_id,cookie)
                except CredentialRejected as exc:
                    if exc.reason != 'invalid_credentials': raise
                    async with self.sessions() as db:
                        a=await self.account(db);state,_=self.validate(a,job_id)
                        state['jobs'][job_id]['password_allowed']=True;p.store(a,state);await db.commit()
            async with self.sessions() as db:
                a=await self.account(db);state,job=self.validate(a,job_id)
                delay=max(0,state['next_retry_at']-time.time())
            await sleep(delay)
            await self.take(job_id,'password_login')
            cookie=await asyncio.wait_for(login(),max(.01,job['expires_at']-time.time()))
            return await self.complete(job_id,cookie)
        except p.StaleAccountOperation:
            return False
        except CredentialRejected as exc:
            await self.stop(job_id,exc.reason); return False
        except asyncio.CancelledError:
            await self.stop(job_id,'cancelled'); raise
        except Exception:
            async with self.sessions() as db:
                a=await self.account(db); reason='proxy' if a.proxy_type not in (None,'none','') else 'network'
            await self.stop(job_id,reason); return False

    async def renew_once(self):
        from common.services.account_credentials import validate_credentials
        async with self.sessions() as db:
            a=await self.account(db)
            cookie,identity=a.cookie,a.unb
            proxy={k:getattr(a,k) for k in ('proxy_type','proxy_host','proxy_port','proxy_user','proxy_pass')}
        # Token 端点本身续签会话 Cookie；无嵌套浏览器、重试或备用出口。
        return await validate_credentials(cookie,identity,proxy)

    async def password_once(self):
        import httpx
        from common.services.account_credentials import CredentialRejected,validate_credentials
        from common.services.xianyu_login.login_do import build_login_form,post_login_do,classify_login_response,LoginBranch
        async with self.sessions() as db:
            a=await self.account(db)
            username,password,identity=a.username,a.login_password,a.unb
            proxy={k:getattr(a,k) for k in ('proxy_type','proxy_host','proxy_port','proxy_user','proxy_pass')}
        if not username or not password: raise CredentialRejected('missing_credentials')
        async with httpx.AsyncClient(proxy=p.proxy_url(proxy),trust_env=False,timeout=30) as client:
            response=await post_login_do(client,build_login_form(username,password))
            if response.status_code==429: raise CredentialRejected('rate_limit')
            if response.status_code>=500: raise CredentialRejected('network')
            result=classify_login_response(response)
            if result.branch in {LoginBranch.SLIDER,LoginBranch.FACE}: raise CredentialRejected('verification')
            if result.branch != LoginBranch.SUCCESS: raise CredentialRejected('invalid_credentials')
            cookie='; '.join(f'{c.name}={c.value}' for c in client.cookies.jar)
        # 成功凭据的首次有效性核实，不再次登录或递归恢复。
        return await validate_credentials(cookie,identity,proxy)
