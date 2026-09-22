"""S1 Chromium -> real rules API -> payment runtime -> visible line facts."""
import asyncio, os, socket, subprocess, tempfile
from pathlib import Path
from unittest.mock import AsyncMock
from fastapi.responses import HTMLResponse, FileResponse
from playwright.async_api import async_playwright, expect
import uvicorn
from test_delivery_rules import DeliveryRuleTests
from app.api.routes import cards
from common.models.xy_order import XYOrder
from common.services.order_delivery_runtime import OrderDeliveryRuntime

async def delayed_read(page, target):
    """Delay one real HTTP response, not component state or API payloads."""
    started, release = asyncio.Event(), asyncio.Event()
    delivered = asyncio.create_task(page.wait_for_event(
        'requestfinished', predicate=lambda request: request.url == target, timeout=20000))
    async def hold(route):
        response = await route.fetch()
        started.set()
        await release.wait()
        await route.fulfill(response=response)
    await page.route(target, hold, times=1)
    return started, release, delivered


async def verify_account_switch(page, url, fixture):
    from common.models.xy_account import XYAccount
    async with fixture.sessions() as session:
        session.add(XYAccount(id=2, owner_id=1, account_id='b', unb='2', cookie='fixture',
                              login_method='cookie', status='active'))
        session.add(XYOrder(id=3, owner_id=1, account_id='b', order_no='account-b-order',
                            item_id='future-item', status='pending_ship', quantity=1))
        await session.commit()
    await fixture.service.reserve(1, 'b', 'account-b-order', 1)
    for account in ('a', 'b'):
        result = await fixture.client.post('/api/v1/orders/commerce/history', json={'account_id': account})
        assert result.status_code == 200, result.text
    region = page.get_by_role('region', name='订单历史和履约检查点')
    selector = page.get_by_label('履约账号', exact=True)
    delayed = [await delayed_read(page, url + '/api/v1/orders/commerce/' + path + '?account_id=a')
               for path in ('intents', 'history')]
    try:
        await page.get_by_role('button', name='刷新任务', exact=True).click()
        await asyncio.gather(*(asyncio.wait_for(started.wait(), 5) for started, _, _ in delayed))
        await selector.select_option('b')
        await region.get_by_role('row').filter(has_text='account-b-order').wait_for()
        for _, release, _ in delayed:
            release.set()
        await asyncio.gather(*(delivered for _, _, delivered in delayed))
        await page.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        await expect(region.get_by_role('row').filter(has_text='account-b-order')).to_be_visible()
        await expect(region.get_by_role('row').filter(has_text='same')).to_have_count(0)
    finally:
        for _, release, delivered in delayed:
            release.set()
            if not delivered.done(): delivered.cancel()
        await asyncio.gather(*(delivered for _, _, delivered in delayed), return_exceptions=True)

    await selector.select_option('a')
    row = region.get_by_role('row').filter(has_text='same')
    await row.wait_for()
    records = (await fixture.client.get('/api/v1/orders/commerce/intents', params={'account_id':'a'})).json()['data']
    started, release, delivered = await delayed_read(
        page, url + '/api/v1/orders/commerce/intents/' + records[0]['id'] + '/content')
    try:
        await row.get_by_role('button', name='查看内容', exact=True).click()
        await asyncio.wait_for(started.wait(), 5)
        await selector.select_option('b')
        await region.get_by_role('row').filter(has_text='account-b-order').wait_for()
        release.set()
        await delivered
        await page.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        await expect(region.get_by_role('button', name='隐藏内容', exact=True)).to_have_count(0)
        await expect(region.get_by_role('row').filter(has_text='account-b-order')).to_be_visible()
    finally:
        release.set()
        if not delivered.done(): delivered.cancel()
        await asyncio.gather(delivered, return_exceptions=True)


async def main():
    fixture=DeliveryRuleTests(); await fixture.asyncSetUp()
    try:
        with tempfile.TemporaryDirectory(prefix='commerce-browser-') as temp:
            root=Path(temp); bundle=root/'app.js'
            subprocess.run(['node','tests/commerce/browser-build.cjs',str(bundle)],check=True)
            app=fixture.app; app.include_router(cards.router,prefix='/api/v1/cards')
            @app.get('/')
            async def index(): return HTMLResponse('<!doctype html><html><body><div id="root"></div><script src="/app.js"></script></body></html>')
            @app.get('/app.js')
            async def js(): return FileResponse(bundle,media_type='text/javascript')
            sock=socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
            server=uvicorn.Server(uvicorn.Config(app,log_level='error')); task=asyncio.create_task(server.serve(sockets=[sock]))
            try:
                while not server.started:
                    if task.done(): await task
                    await asyncio.sleep(.01)
                async with async_playwright() as pw:
                    browser=await pw.chromium.launch(executable_path=str(sorted((Path.home() / '.cache/ms-playwright').glob('chromium-*/chrome-*/chrome'))[-1]),headless=True,args=['--no-sandbox'])
                    page=await browser.new_page(); page.set_default_timeout(6000); errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    url=f'http://127.0.0.1:{port}'
                    await page.route('**/*',lambda r:r.continue_() if r.request.url.startswith(url+'/') else r.abort())
                    await page.goto(url)
                    ui=page.get_by_role('region',name='标题发货规则')
                    await ui.get_by_label('标题关键词').fill('软件')
                    await ui.get_by_label('使用卡券').select_option('1')
                    await ui.get_by_label('发放倍数').fill('2')
                    await ui.get_by_label('规则启用').check()
                    await ui.get_by_role('button',name='新增发货规则',exact=True).click()
                    await expect(ui.get_by_role('row').filter(has_text='软件')).to_contain_text('× 2')
                    await page.get_by_label('规则预览订单号').fill('same')
                    await page.get_by_role('button',name='预览匹配规则',exact=True).click()
                    await page.get_by_text('标题规则及SKU命中',exact=False).wait_for()
                    send=AsyncMock(return_value={'delivery_state':'confirmed'}); confirm=AsyncMock(return_value='not_sent')
                    await OrderDeliveryRuntime(fixture.sessions).handle(account_id='a',order_no='same',item_id='future-item',buyer_id='buyer',chat_id='chat',send_text=send,send_image=send,confirm=confirm,mode='send_first')
                    assert [c.args[0] for c in send.call_args_list]==['A','B','C','D']
                    await page.get_by_role('button',name='刷新任务',exact=True).click()
                    await page.get_by_text('尝试 1/4',exact=True).wait_for()
                    await expect(page.get_by_role('button',name='仅补确认',exact=True)).to_be_disabled()
                    row=ui.get_by_role('row').filter(has_text='软件')
                    await row.get_by_role('button',name='编辑',exact=True).click()
                    await ui.get_by_label('标题关键词').fill('软件半年卡')
                    await ui.get_by_role('button',name='保存发货规则',exact=True).click()
                    await ui.get_by_role('row').filter(has_text='软件半年卡').get_by_role('button',name='停用',exact=True).click()
                    await expect(ui.get_by_role('row').filter(has_text='软件半年卡')).to_contain_text('已停用')
                    # Rule writes do not expose stored stock or supplier secrets.
                    assert 'A\nB\nC' not in await page.locator('body').inner_text()
                    await verify_account_switch(page, url, fixture)
                    assert not errors,errors
                    await page.screenshot(path=str(Path(os.environ.get('XYMB_VERIFY_OUTPUT',temp))/'commerce-rules.png'),full_page=True)
                    await browser.close()
                print('commerce browser: rule create/edit/disable, actual quantity, confirmation budget, delayed account-switch list/detail isolation passed')
            finally:
                server.should_exit=True; await asyncio.wait_for(task,10)
    finally: await fixture.asyncTearDown()

if __name__=='__main__': asyncio.run(main())
