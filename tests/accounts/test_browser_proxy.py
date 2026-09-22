"""S1/S2: real login browser traffic is observed at a fixed authenticated proxy."""
import asyncio
import base64
import os
from urllib.parse import urlsplit
from unittest.mock import patch
from aiohttp import ClientSession, web
from test_runtime import DatabaseCase
from common.models.xy_account import XYAccount
from common.services import account_browser


class BrowserProxyTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.received = asyncio.Event()
        self.origin_requests, self.proxy_requests = [], []
        async def origin(request):
            self.origin_requests.append(request.path)
            if request.method == 'POST':
                self.form = dict(await request.post()); self.received.set()
                return web.Response(text='verification fixture')
            return web.Response(text='''<form method="post" action="/submitted">
                <input id="fm-login-id" name="username"><input id="fm-login-password" name="password">
                <button class="password-login">login</button></form>''',content_type='text/html')
        app=web.Application();app.router.add_route('*','/{tail:.*}',origin)
        self.origin=web.AppRunner(app);await self.origin.setup()
        site=web.TCPSite(self.origin,'127.0.0.1',0);await site.start()
        self.origin_port=site._server.sockets[0].getsockname()[1]
        self.http=ClientSession(trust_env=False)
        async def proxy(request):
            target=urlsplit(request.raw_path)
            if target.hostname != 'browser.fixture.test':
                return web.Response(status=403)
            self.proxy_requests.append(request.raw_path)
            expected='Basic '+base64.b64encode(b'proxy-fixture:proxy-password').decode()
            if request.headers.get('Proxy-Authorization') != expected:
                return web.Response(status=407,headers={'Proxy-Authenticate':'Basic realm="fixture"'})
            async with self.http.request(request.method,f'http://127.0.0.1:{self.origin_port}'+target.path,
                data=await request.read(),headers={'Content-Type':request.headers.get('Content-Type','')},
                allow_redirects=False) as response:
                return web.Response(status=response.status,body=await response.read(),
                    headers={'Content-Type':response.headers.get('Content-Type','text/plain')})
        app=web.Application();app.router.add_route('*','/{tail:.*}',proxy)
        self.proxy=web.AppRunner(app);await self.proxy.setup()
        site=web.TCPSite(self.proxy,'127.0.0.1',0);await site.start()
        self.proxy_port=site._server.sockets[0].getsockname()[1]
        self.manager=account_browser.AccountBrowserSessions(self.factory)

    async def asyncTearDown(self):
        for sid in list(self.manager.sessions):
            await self.manager.close(sid)
        await self.http.close();await self.proxy.cleanup();await self.origin.cleanup()
        await super().asyncTearDown()

    async def set_proxy(self,password='proxy-password'):
        async with self.factory() as db:
            row=await db.get(XYAccount,1)
            row.proxy_type='http';row.proxy_host='127.0.0.1';row.proxy_port=self.proxy_port
            row.proxy_user='proxy-fixture';row.proxy_pass=password
            await db.commit()

    async def test_login_browser_uses_account_proxy_despite_conflicting_environment(self):
        await self.set_proxy()
        with patch.object(account_browser,'LOGIN_URL','http://browser.fixture.test/login',create=True), \
             patch.dict(os.environ,{'HTTP_PROXY':'http://127.0.0.1:1','HTTPS_PROXY':'http://127.0.0.1:1'}):
            job=await self.manager.open('fixture',7,'seller-fixture','password-fixture')
            await asyncio.wait_for(self.received.wait(),3)
        self.assertEqual(self.form,{'username':'seller-fixture','password':'password-fixture'})
        self.assertIn('/submitted',self.origin_requests)
        self.assertTrue(any('/submitted' in url for url in self.proxy_requests))
        self.assertEqual((await self.manager.status(job['id'],7))['kind'],'verification')
        await self.manager.cancel(job['id'],7)

    async def test_proxy_auth_failure_does_not_contact_origin(self):
        await self.set_proxy('incorrect')
        with patch.object(account_browser,'LOGIN_URL','http://browser.fixture.test/login',create=True):
            job=await self.manager.open('fixture',7,'seller-fixture','password-fixture')
        self.assertTrue(self.proxy_requests)
        self.assertEqual(self.origin_requests,[])
        state = await self.manager.status(job['id'],7)
        self.assertNotEqual(state['status'],'verified')
        self.assertEqual(state.get('reason'),'proxy_error')
        await self.manager.cancel(job['id'],7)

    async def test_cancel_reaps_owned_browser_and_driver_without_a_browser_pid_property(self):
        import psutil
        process = psutil.Process()
        before = {child.pid for child in process.children(recursive=True)}
        await self.set_proxy()
        with patch.object(account_browser, 'LOGIN_URL', 'http://browser.fixture.test/login'):
            job = await self.manager.open('fixture', 7, 'seller-fixture', 'password-fixture')
            await asyncio.wait_for(self.received.wait(), 3)
        owned = [child for child in process.children(recursive=True) if child.pid not in before]
        self.assertTrue(owned, 'real Playwright driver and Chromium must have started')
        await self.manager.cancel(job['id'], 7)
        def running(child):
            try:
                return child.is_running() and child.status() != psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return False
        for _ in range(100):
            if not any(running(child) for child in owned):
                break
            await asyncio.sleep(.05)
        self.assertFalse(any(running(child) for child in owned), 'owned browser or driver still running')
