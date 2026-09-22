"""账号人工验证：同一个异步浏览器上下文，归属/版本校验，主动截止释放。"""
import asyncio
import time
from sqlalchemy import select
from common.models.xy_account import XYAccount
from common.services import account_policy as p
from common.services.account_credentials import validate_credentials

LOGIN_URL = 'https://passport.goofish.com/mini_login.htm?lang=zh_cn&appName=xianyu&appEntrance=web&styleType=vertical'


class AccountBrowserSessions:
    def __init__(self, sessions=None):
        if sessions is None:
            from common.db.session import async_session_maker
            sessions=async_session_maker
        self.db_sessions=sessions
        self.sessions={}
        self.lock=asyncio.Lock()

    async def _account(self, db, account_id, owner):
        a=(await db.execute(select(XYAccount).where(XYAccount.account_id==account_id,
             XYAccount.owner_id==owner).with_for_update())).scalar_one_or_none()
        if not a: raise PermissionError('账号会话不存在')
        return a

    async def bind(self, account_id, owner, context, page, *, lifetime=900, job_id=None):
        async with self.lock:
            async with self.db_sessions() as db:
                a=await self._account(db,account_id,owner)
                job=p.get_job(a,job_id,owner_id=owner) if job_id else p.start_job(a,owner_id=owner,kind='verification',platform_expires_at=time.time()+min(lifetime,900))
                if job['id'] in self.sessions or job['status'] in p.TERMINAL or job['kind'] != 'verification':
                    await context.close(); raise ValueError('账号已有验证任务')
                await db.commit()
            self.sessions[job['id']]={'account_id':account_id,'owner':owner,'context':context,'page':page,'job':job,'lock':asyncio.Lock()}
            self.sessions[job['id']]['expiry']=asyncio.create_task(self._expire(job['id'],job['expires_at']))
            return job

    async def _expire(self,sid,expires):
        await asyncio.sleep(max(0,expires-time.time()))
        row=self.sessions[sid]
        await self.status(sid,row['owner'])

    async def status(self,sid,owner):
        row=self.sessions.get(sid)
        if not row or row['owner']!=owner: raise PermissionError('账号会话不存在')
        async with self.db_sessions() as db:
            a=await self._account(db,row['account_id'],owner)
            job=p.get_job(a,sid,owner_id=owner)
            state=p.snapshot(a)
            if job['status'] not in p.TERMINAL and any(job[k]!=state[k] for k in ('credential_version','config_version','generation')):
                state['jobs'][sid]['status']='superseded';p.store(a,state);job=state['jobs'][sid]
            await db.commit()
        row['job']=job
        if job['status'] in p.TERMINAL: await self.close(sid)
        return job

    async def _live(self,sid,owner):
        job=await self.status(sid,owner)
        if job['status'] in p.TERMINAL: raise ValueError('会话已结束')
        return self.sessions[sid]

    async def screenshot(self,sid,owner):
        row=await self._live(sid,owner)
        async with row['lock']:
            return await row['page'].screenshot(type='png',full_page=False)

    async def control(self,sid,owner,command):
        row=await self._live(sid,owner)
        async with row['lock']:
            page=row['page']; action=command.get('action')
            if action=='click':
                x,y=command.get('x'),command.get('y')
                if any(type(v) not in (int,float) or not 0<=v<=4096 for v in (x,y)): raise ValueError('坐标无效')
                await page.mouse.click(x,y)
            elif action=='drag':
                points = command.get('points')
                if not isinstance(points, list) or not 2 <= len(points) <= 128:
                    raise ValueError('拖动坐标数量无效')
                for point in points:
                    if not isinstance(point, dict) or any(
                        type(point.get(key)) not in (int, float) or not 0 <= point[key] <= 4096
                        for key in ('x', 'y')
                    ):
                        raise ValueError('拖动坐标无效')
                await page.mouse.move(points[0]['x'], points[0]['y'])
                await page.mouse.down()
                try:
                    for point in points[1:]:
                        await page.mouse.move(point['x'], point['y'])
                finally:
                    await page.mouse.up()
            elif action=='text':
                text=command.get('text','')
                if not isinstance(text,str) or len(text)>2048: raise ValueError('输入过长')
                await page.keyboard.insert_text(text)
            elif action=='key' and command.get('key') in {'Enter','Tab','Backspace','Escape','ArrowLeft','ArrowRight'}:
                await page.keyboard.press(command['key'])
            else: raise ValueError('未知浏览器操作')

    async def cancel(self,sid,owner):
        row=await self._live(sid,owner)
        async with self.db_sessions() as db:
            a=await self._account(db,row['account_id'],owner);p.cancel_job(a,sid,owner_id=owner);await db.commit()
        await self.close(sid)

    async def close(self,sid):
        row=self.sessions[sid]
        async with row['lock']:
            ctx=row.pop('context',None);row['page']=None
            expiry=row.get('expiry')
            if expiry and expiry is not asyncio.current_task(): expiry.cancel()
            if ctx:
                try: await ctx.close()
                finally:
                    browser=row.pop('browser',None)
                    if browser: await browser.close()
                    pw=row.pop('playwright',None)
                    if pw: await pw.stop()

    async def complete(self,sid,cookie):
        row=self.sessions[sid]
        if (await self.status(sid,row['owner']))['status'] in p.TERMINAL: return False
        async with self.db_sessions() as db:
            a=await self._account(db,row['account_id'],row['owner'])
            proxy={k:getattr(a,k) for k in ('proxy_type','proxy_host','proxy_port','proxy_user','proxy_pass')}
            identity=a.unb
        checked=await asyncio.wait_for(validate_credentials(cookie,identity,proxy),timeout=max(.01,min(60,row['job']['expires_at']-time.time())))
        async with self.db_sessions() as db:
            a=await self._account(db,row['account_id'],row['owner']);accepted=p.complete_job(a,sid,checked);await db.commit()
        await self.close(sid);return accepted

    async def finish_browser(self,sid,owner):
        row=await self._live(sid,owner)
        async with row['lock']:
            cookies=await row['context'].cookies(['https://www.goofish.com','https://passport.goofish.com'])
        return await self.complete(sid,'; '.join(f"{c['name']}={c['value']}" for c in cookies))

    async def open(self,account_id,owner,username,password):
        # 先落任务再打开浏览器。新标签/浏览器失败也保留可审计终态。
        async with self.db_sessions() as db:
            a=await self._account(db,account_id,owner)
            proxy=p.browser_proxy({k:getattr(a,k) for k in ('proxy_type','proxy_host','proxy_port','proxy_user','proxy_pass')})
            job=p.start_job(a,owner_id=owner,kind='verification')
            await db.commit()
        if job['id'] in self.sessions: return job
        from playwright.async_api import async_playwright
        from common.utils.browser_utils import get_chromium_executable_path
        pw=None;browser=None;ctx=None
        proxy_rejected = False

        def observe_response(response):
            nonlocal proxy_rejected
            # Chromium reports HTTP proxy authentication failures as the generic
            # ERR_HTTP_RESPONSE_CODE_FAILURE. The actual 407 distinguishes them
            # from a failure returned by the destination server.
            if proxy is not None and response.status == 407:
                proxy_rejected = True

        try:
            pw=await async_playwright().start()
            browser=await pw.chromium.launch(headless=True,proxy=proxy,args=['--no-sandbox'] + (['--no-proxy-server'] if proxy is None else []),executable_path=get_chromium_executable_path())
            ctx=await browser.new_context(viewport={'width':1024,'height':768})
            page=await ctx.new_page()
            page.on('response', observe_response)
            await self.bind(account_id,owner,ctx,page,job_id=job['id'])
            self.sessions[job['id']]['playwright']=pw
            self.sessions[job['id']]['browser']=browser
            await page.goto(LOGIN_URL,wait_until='domcontentloaded',timeout=30000)
            # 标准登录表单；出现额外验证时留在原页面，由持有人操作。
            await page.locator('#fm-login-id').fill(username,timeout=10000)
            await page.locator('#fm-login-password').fill(password,timeout=10000)
            await page.locator('.password-login').click(timeout=10000)
            return job
        except Exception as exc:
            proxy_failure = proxy_rejected or (proxy is not None and any(code in str(exc) for code in (
                'ERR_PROXY', 'ERR_TUNNEL_CONNECTION_FAILED', 'ERR_SOCKS_CONNECTION_FAILED',
                'ERR_INVALID_AUTH_CREDENTIALS')))
            if proxy_failure:
                async with self.db_sessions() as db:
                    a = await self._account(db, account_id, owner)
                    state = p.snapshot(a)
                    current = state['jobs'].get(job['id'])
                    if (current and current['status'] not in p.TERMINAL and
                            all(current[key] == state[key] for key in ('generation','config_version','credential_version'))):
                        current['reason'] = 'proxy_error'
                        state.update(business_state='proxy_error', reason='proxy_error')
                        p.store(a, state)
                        await db.commit()
            if job['id'] in self.sessions:
                # 页面已创建就保留给用户查看和操作，截止任务负责释放。
                return job
            if ctx: await ctx.close()
            if browser: await browser.close()
            if pw: await pw.stop()
            async with self.db_sessions() as db:
                a=await self._account(db,account_id,owner);p.fail_job(a,job['id']);await db.commit()
            raise
