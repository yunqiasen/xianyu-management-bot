"""Chromium -> real account policy UI -> FastAPI -> SQL -> local consumer endpoints."""
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from unittest.mock import patch
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from playwright.async_api import async_playwright
from test_config_inheritance import ConfigInheritanceTests
from common.services.account_configuration import ConfigurationApplyRequest, ConfigurationStore


async def main():
    fixture = ConfigInheritanceTests()
    await fixture.asyncSetUp()
    try:
        with tempfile.TemporaryDirectory(prefix='xymb-policy-browser-') as temp:
            bundle = Path(temp) / 'app.js'
            subprocess.run(['node','tests/accounts/policy-build.cjs', str(bundle)], check=True)
            app=FastAPI()
            app.mount('/api/v1', fixture.app)
            failed={'websocket':True}
            apply_lock=asyncio.Lock()
            @app.get('/')
            async def index():
                return HTMLResponse('<html><body><div id="root"></div><script src="/app.js"></script></body></html>')
            @app.get('/app.js')
            async def script(): return FileResponse(bundle, media_type='text/javascript')
            @app.post('/{consumer}/internal/account-configuration')
            async def apply(consumer: str, body: ConfigurationApplyRequest):
                if failed.get(consumer): return JSONResponse({'error':'fixture-offline'}, status_code=503)
                # SQLite ignores SELECT FOR UPDATE; serialize this S2 consumer
                # fixture as MySQL's account-row lock does. Native concurrency is
                # verified separately in test_infrastructure.
                async with apply_lock:
                    return await ConfigurationStore(fixture.factory).acknowledge(body.owner_id, body.account_id, body.config_version, consumer)
            sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            server=uvicorn.Server(uvicorn.Config(app, log_level='error'))
            task=asyncio.create_task(server.serve(sockets=[sock]))
            try:
                while not server.started:
                    if task.done(): await task
                    await asyncio.sleep(.01)
                url=f'http://127.0.0.1:{port}'
                from app.core.config import get_settings
                with patch.object(get_settings(),'internal_api_token','fixture-' * 8), patch.object(get_settings(),'websocket_service_url', url+'/websocket'), patch.object(get_settings(),'scheduler_service_url',url+'/scheduler'):
                    async with async_playwright() as pw:
                        browser=await pw.chromium.launch(executable_path=str(sorted((Path.home() / '.cache/ms-playwright').glob('chromium-*/chrome-*/chrome'))[-1]), headless=True, args=['--no-sandbox'])
                        page=await browser.new_page()
                        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        await page.route('**/*',lambda route:route.continue_() if route.request.url.startswith(url+'/') else route.abort())
                        await page.goto(url)
                        await page.get_by_text('未设置业务频率', exact=False).wait_for()
                        await page.get_by_label('最小请求间隔（秒）').fill('15')
                        await page.get_by_role('button',name='保存频率',exact=True).click()
                        await page.get_by_text('配置间隔：15 秒',exact=False).wait_for()
                        await page.get_by_role('button',name='应用待生效配置',exact=True).click()
                        await page.get_by_text('配置部分应用，待补齐：websocket',exact=False).wait_for()
                        failed['websocket']=False
                        await page.get_by_role('button',name='应用待生效配置',exact=True).click()
                        await page.get_by_text('三个服务已应用此版本',exact=True).wait_for()
                        typed=page.get_by_role('region',name='配置继承')
                        await typed.get_by_label('配置作用域').select_option('user')
                        await typed.get_by_label('配置项',exact=True).select_option('pause_duration')
                        await typed.get_by_label('配置值',exact=True).fill('18')
                        await typed.get_by_role('button',name='保存显式值',exact=True).click()
                        await typed.get_by_text('配置已保存',exact=False).wait_for()
                        await typed.get_by_label('配置作用域').select_option('account')
                        await typed.get_by_role('button',name='继承上级',exact=True).click()
                        await typed.get_by_text('有效值：18 · 来源：用户默认',exact=True).wait_for()
                        await typed.get_by_label('配置值',exact=True).fill('0')
                        await typed.get_by_role('button',name='保存显式值',exact=True).click()
                        await typed.get_by_text('有效值：0 · 来源：账号显式',exact=True).wait_for()
                        assert await typed.get_by_label('配置作用域').locator('option[value=system]').count()==0
                        await typed.get_by_role('button',name='应用此账号配置',exact=True).click()
                        await typed.get_by_text('三个服务已应用此版本',exact=True).wait_for()
                        final=(await fixture.client.get('/cookies/fixture/configuration/effective')).json()['data']
                        assert final['values']['pause_duration']=={'value':0,'source':'account','inherited':False},final
                        assert not final['pending_consumers'], final
                        assert not errors,errors
                        result=(await fixture.client.get('/cookies/fixture/request-policy')).json()['data']
                        assert result['effective_interval_seconds']==15 and result['pending_consumers']==[],result
                        await page.screenshot(path=str(Path(os.environ.get('XYMB_VERIFY_OUTPUT',temp))/'account-request-policy.png'),full_page=True)
                        await browser.close()
                print('PASS account policy, partial apply, typed user defaults, account inheritance, explicit zero and shared version in Chromium/FastAPI/SQL')
            finally:
                server.should_exit=True
                await task
    finally:
        await fixture.asyncTearDown()


if __name__=='__main__': asyncio.run(main())
