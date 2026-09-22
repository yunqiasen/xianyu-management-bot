"""S1 browser filter management -> shared account scope -> durable conversation pause."""
import asyncio, os, socket, tempfile, subprocess
from pathlib import Path
from fastapi.responses import HTMLResponse, FileResponse
from playwright.async_api import async_playwright, expect
import uvicorn
from api_suite import ManagementApiTests

async def main():
    fixture=ManagementApiTests();await fixture.asyncSetUp()
    try:
        with tempfile.TemporaryDirectory(prefix='replies-browser-') as temp:
            root=Path(temp);bundle=root/'app.js'
            subprocess.run(['node','tests/replies/browser-build.cjs',str(bundle)],check=True)
            app=fixture.app; app.include_router(fixture.api.router,prefix='/api/v1/chat-new')
            @app.get('/')
            async def index(): return HTMLResponse('<!doctype html><html><body><div id="root"></div><script src="/app.js"></script></body></html>')
            @app.get('/app.js')
            async def js(): return FileResponse(bundle,media_type='text/javascript')
            sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            server=uvicorn.Server(uvicorn.Config(app,log_level='error'));task=asyncio.create_task(server.serve(sockets=[sock]))
            try:
                while not server.started:
                    if task.done(): await task
                    await asyncio.sleep(.01)
                async with async_playwright() as pw:
                    browser=await pw.chromium.launch(executable_path=str(sorted((Path.home() / '.cache/ms-playwright').glob('chromium-*/chrome-*/chrome'))[-1]),headless=True,args=['--no-sandbox'])
                    page=await browser.new_page();page.set_default_timeout(6000);errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    url=f'http://127.0.0.1:{port}'
                    await page.route('**/*',lambda r:r.continue_() if r.request.url.startswith(url+'/') else r.abort())
                    await page.goto(url)
                    await page.locator('summary').filter(has_text='高级过滤').click()
                    await page.get_by_label('过滤内容',exact=True).fill('人工')
                    await page.get_by_label('本人全部账号（含今后新增）',exact=True).check()
                    await page.get_by_label('暂停会话',exact=True).check()
                    await page.get_by_label('过滤暂停分钟数',exact=True).fill('7')
                    await page.get_by_role('button',name='保存过滤',exact=True).click()
                    await page.get_by_text('全部账号 / user / 人工',exact=False).wait_for()
                    records=(await fixture.client.get('/reply-controls/a/filters')).json()['data']
                    assert records[0]['pause_minutes']==7 and records[0]['all_accounts']
                    await page.get_by_role('button',name='停用',exact=True).click()
                    await page.get_by_role('button',name='启用',exact=True).wait_for()
                    assert not (await fixture.client.get('/reply-controls/a/filters')).json()['data'][0]['enabled']
                    assert not errors,errors
                    await page.screenshot(path=str(Path(os.environ.get('XYMB_VERIFY_OUTPUT',temp))/'reply-filters.png'),full_page=True)
                    await browser.close()
                print('reply controls browser: global scope, pause duration and disable passed')
            finally:
                server.should_exit=True;await asyncio.wait_for(task,10)
    finally:await fixture.asyncTearDown()

if __name__=='__main__':asyncio.run(main())
