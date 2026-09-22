import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
from test_runtime import DatabaseCase
from common.models.xy_account import XYAccount
from common.services import account_policy as p

class BrowserSessionTests(DatabaseCase):
    async def manager(self):
        from common.services.account_browser import AccountBrowserSessions
        return AccountBrowserSessions(self.factory)

    async def test_same_browser_control_owner_cancel_and_late_result(self):
        m=await self.manager(); context=AsyncMock(); page=AsyncMock()
        page.mouse=AsyncMock(); page.keyboard=AsyncMock(); page.screenshot.return_value=b'png'
        job=await m.bind('fixture',7,context,page)
        self.assertEqual(await m.screenshot(job['id'],7),b'png')
        await m.control(job['id'],7,{'action':'click','x':12,'y':24})
        page.mouse.click.assert_awaited_once_with(12,24)
        with self.assertRaises(PermissionError): await m.screenshot(job['id'],8)
        await m.cancel(job['id'],7)
        context.close.assert_awaited_once()
        self.assertFalse(await m.complete(job['id'],'unb=101; token=late'))
        async with self.factory() as db:
            self.assertEqual((await db.get(XYAccount,1)).cookie,'unb=101; token=old')

    async def test_timeout_closes_without_polling_and_clears_page(self):
        m=await self.manager(); ctx=AsyncMock(); page=AsyncMock()
        job=await m.bind('fixture',7,ctx,page,lifetime=.02)
        await asyncio.sleep(.05)
        ctx.close.assert_awaited_once()
        self.assertIsNone(m.sessions[job['id']]['page'])
        self.assertEqual((await m.status(job['id'],7))['status'],'expired')

    async def test_success_revalidates_and_keeps_disabled_account(self):
        m=await self.manager();ctx=AsyncMock();page=AsyncMock()
        job=await m.bind('fixture',7,ctx,page)
        async with self.factory() as db:
            a=await db.get(XYAccount,1); a.status='disabled'; await db.commit()
        from unittest.mock import patch
        with patch('common.services.account_browser.validate_credentials',AsyncMock(return_value='unb=101; token=checked')) as check:
            self.assertTrue(await m.complete(job['id'],'unb=101; token=browser'))
            check.assert_awaited_once()
        async with self.factory() as db:
            a=await db.get(XYAccount,1); self.assertEqual(a.status,'disabled');self.assertEqual(a.cookie,'unb=101; token=checked')
        ctx.close.assert_awaited_once()

    async def test_real_chromium_controls_original_local_page(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            from common.utils.browser_utils import get_chromium_executable_path
            browser=await pw.chromium.launch(headless=True,args=['--no-sandbox'],executable_path=get_chromium_executable_path())
            context=await browser.new_context(viewport={'width':400,'height':300})
            await context.route('**/*',lambda route: route.abort())
            page=await context.new_page()
            await page.set_content('<input style="position:absolute;left:10px;top:10px;width:200px;height:40px" id="field">')
            manager=await self.manager();job=await manager.bind('fixture',7,context,page)
            try:
                await manager.control(job['id'],7,{'action':'click','x':40,'y':30})
                await manager.control(job['id'],7,{'action':'text','text':'fixture'})
                self.assertEqual(await page.locator('#field').input_value(),'fixture')
                self.assertTrue((await manager.screenshot(job['id'],7)).startswith(b'\x89PNG'))
                await manager.cancel(job['id'],7)
                self.assertTrue(page.is_closed())
            finally: await browser.close()

    async def test_manual_drag_reaches_original_browser_and_releases_pointer(self):
        from playwright.async_api import async_playwright
        from common.utils.browser_utils import get_chromium_executable_path
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'],
                executable_path=get_chromium_executable_path())
            context = await browser.new_context(viewport={'width':400,'height':300})
            await context.route('**/*', lambda route: route.abort())
            page = await context.new_page()
            await page.set_content('''<script>
                window.moves=[];
                document.addEventListener('mousemove', e => { if(e.buttons) moves.push([e.clientX,e.clientY]); });
                document.addEventListener('mouseup', e => { window.released=true; });
            </script>''')
            manager = await self.manager()
            job = await manager.bind('fixture', 7, context, page)
            try:
                await manager.control(job['id'], 7, {'action':'drag', 'points':[
                    {'x':20,'y':30}, {'x':90,'y':30}, {'x':180,'y':30}]})
                self.assertEqual(await page.evaluate('moves'), [[90,30],[180,30]])
                self.assertTrue(await page.evaluate('released'))
                with self.assertRaises(ValueError):
                    await manager.control(job['id'], 7, {'action':'drag', 'points':[
                        {'x':20,'y':30}, {'x':float('nan'),'y':30}]})
                with self.assertRaises(PermissionError):
                    await manager.control(job['id'], 8, {'action':'drag', 'points':[
                        {'x':20,'y':30}, {'x':180,'y':30}]})
            finally:
                await manager.cancel(job['id'], 7)
                await browser.close()
