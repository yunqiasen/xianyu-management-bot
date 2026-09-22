"""Actual Chromium -> React pages -> FastAPI -> SQLite; outbound platform methods are fixtures."""
import asyncio, json, os, socket, tempfile, subprocess
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from unittest.mock import AsyncMock, patch
import uvicorn
from fastapi.responses import HTMLResponse, FileResponse
from playwright.async_api import async_playwright
from sqlalchemy import select
import test_api as fixtures
from app.api.routes import auto_rate
from common.models.xy_order import XYOrder
from common.models.xy_catalog_item import XYCatalogItem
from common.models.auto_rate_config import AutoRateConfig
from common.models.scheduled_polish_log import ScheduledPolishLog
from common.models.publish_log import PublishLog
from common.services.product_batch_service import ProductBatchService
from common.services import publish_execution_service as execution

async def main():
    fixture=fixtures.ProductApiTests()
    await fixture.asyncSetUp()
    tmp=tempfile.TemporaryDirectory(prefix='products-browser-')
    bundle=Path(tmp.name)/'app.js'
    subprocess.run(['node','tests/products/browser-build.cjs',str(bundle)],check=True)
    app=fixture.app
    app.include_router(auto_rate.router,prefix='/api/v1/auto-rate')
    @app.get('/')
    async def index(): return HTMLResponse('<!doctype html><html><body><div id="root"></div><script src="/app.js"></script></body></html>')
    @app.get('/app.js')
    async def javascript(): return FileResponse(bundle,media_type='text/javascript')
    @app.get('/api/v1/cookies/options')
    async def accounts(): return [{'id':'a1','note':'fixture-account','enabled':True,'auto_red_flower':True}]
    async with fixture.engine.begin() as conn:
        for model in (XYOrder,XYCatalogItem,AutoRateConfig,ScheduledPolishLog): await conn.run_sync(model.__table__.create)
    async with fixture.sessions() as db:
        from common.models.xy_account import XYAccount
        account=await db.get(XYAccount,1);account.auto_red_flower=True
        db.add_all([AutoRateConfig(account_id='a1',enabled=True,rate_type='text',text_content='不错的买家'),
            XYOrder(owner_id=1,account_id='a1',order_no='o1',status='completed'),
            XYOrder(owner_id=1,account_id='a1',order_no='o2',status='completed'),
            XYCatalogItem(owner_id=1,account_pk=1,item_id='i1',title='fixture',created_at=datetime.now(),updated_at=datetime.now())])
        await db.commit()
        batch=await ProductBatchService(db).create(1,['a1'],[fixture.material() for _ in range(4)])
        bid=batch.id
        logs=list((await db.execute(select(PublishLog).order_by(PublishLog.id))).scalars())
        for row,state in zip(logs,['success','failed','unknown','pending']): row.status=state
        logs[0].item_id='already-published'
        await db.commit()
    address=SimpleNamespace(apply_to_item_data=lambda d:dict(d),to_log_fields=lambda:{})
    external=AsyncMock(return_value={'success':True,'item_id':'published-on-retry'})
    polish=AsyncMock(side_effect=TimeoutError())
    rate=AsyncMock(return_value={'success':True,'message':'评价成功'})
    flower=AsyncMock(side_effect=TimeoutError())
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started: await asyncio.sleep(.01)
    errors=[]
    try:
        with patch.object(execution,'async_session_maker',fixture.sessions), patch('common.db.session.async_session_maker',fixture.sessions), \
             patch.object(execution.PublishAddressService,'resolve_publish_address',new=AsyncMock(return_value=address)), \
             patch.object(execution,'detect_publish_account_capability',new=AsyncMock(return_value={'success':True,'is_fish_shop':True})), \
             patch.object(execution,'ensure_publish_capability_reliable',new=lambda r:r), \
             patch.object(execution,'_sync_account_items_after_publish',new=AsyncMock(return_value={})), \
             patch.object(execution,'publish_single_item',new=external), \
             patch('common.services.rate_service.RateService._rate_buyer_impl',new=rate), \
             patch('common.services.product_red_flower.request_red_flower',new=flower), \
             patch('common.services.product_polish_service.ProductPolishTransport.send',new=polish):
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(executable_path=str(sorted((Path.home() / '.cache/ms-playwright').glob('chromium-*/chrome-*/chrome'))[-1]),headless=True,args=['--no-sandbox'])
                page=await browser.new_page()
                page.on('pageerror',lambda error:errors.append(str(error)))
                # Every URL is loopback; never allow a component to navigate to platform URLs.
                await page.route('**/*',lambda route:route.continue_() if route.request.url.startswith(f'http://127.0.0.1:{port}') else route.abort())
                await page.add_init_script(f"sessionStorage.setItem('batch_publish_active_batch_id',{json.dumps(bid)})")
                await page.goto(f'http://127.0.0.1:{port}/')
                await page.get_by_role('button',name='取消未发项',exact=True).click()
                await page.get_by_role('button',name='仅重试明确失败项',exact=True).click()
                await page.wait_for_timeout(300)
                status=(await fixture.client.get(f'/api/v1/product-publish/publish/batch/{bid}/status')).json()['data']
                assert (status['success'],status['unknown'],status['cancelled'])==(2,1,1),status
                assert external.await_count==1
                await page.get_by_role('button',name='logs',exact=True).click()
                await page.get_by_role('button',name='核对未发布',exact=True).wait_for()
                page.once('dialog',lambda dialog:dialog.accept('已核对平台商品列表和发布时间，确认没有发布'))
                await page.get_by_role('button',name='核对未发布',exact=True).click()
                await page.wait_for_timeout(200)
                assert not await page.get_by_role('button',name='核对未发布',exact=True).count()
                await page.get_by_role('button',name='查看操作证据').first.click()
                await page.get_by_text('操作证据',exact=False).first.wait_for()
                await page.get_by_role('button',name='rate',exact=True).click()
                await page.get_by_label('执行账号').select_option('a1')
                await page.get_by_text('评价模板（激活不改变原开关）',exact=True).click()
                await page.get_by_label('模板名称').fill('browser-template')
                await page.get_by_label('评价模板内容').fill('浏览器验收评价内容')
                await page.get_by_role('button',name='新建模板',exact=True).click()
                await page.get_by_role('button',name='激活',exact=True).click()
                await page.get_by_text('browser-template（已激活）',exact=True).wait_for()
                await page.get_by_label('订单号',exact=True).fill('o1')
                await page.get_by_role('button',name='立即执行指定订单',exact=True).click()
                await page.get_by_role('cell',name='成功',exact=True).wait_for()
                await page.get_by_role('button',name='立即执行指定订单',exact=True).click()
                await page.get_by_role('cell',name='已处理',exact=True).wait_for()
                assert rate.await_count==1
                await page.get_by_role('button',name='flower',exact=True).click()
                await page.get_by_label('执行账号').select_option('a1')
                await page.get_by_label('订单号',exact=True).fill('o2')
                await page.get_by_role('button',name='立即执行指定订单',exact=True).click()
                await page.get_by_role('cell',name='结果待核实',exact=True).wait_for()
                await page.get_by_role('button',name='立即执行指定订单',exact=True).click()
                await page.get_by_role('cell',name='跳过',exact=True).wait_for()
                assert flower.await_count==1
                await page.get_by_role('button',name='polish',exact=True).click()
                await page.locator('summary').first.click()
                await page.get_by_role('button',name='立即擦亮',exact=True).click()
                await page.locator('summary').filter(has_text='unknown').wait_for()
                await page.get_by_role('button',name='立即擦亮',exact=True).click()
                await page.wait_for_timeout(200)
                assert polish.await_count==1
                assert not errors,errors
                await page.screenshot(path=str(Path(os.environ.get('XYMB_VERIFY_OUTPUT', tmp.name)) / 'browser-polish.png'),full_page=True)
                await browser.close()
        print('PASS Chromium -> React -> FastAPI -> SQLite: batch cancel/failed-only retry, reconcile evidence, template activation, rate duplicate, flower unknown stop, manual polish persistence')
    finally:
        server.should_exit=True
        await task
        await fixture.asyncTearDown()
        tmp.cleanup()

if __name__=='__main__': asyncio.run(main())
