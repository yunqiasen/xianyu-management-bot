"""S1 UI: user's own pointer gestures map to the original browser coordinates."""
from pathlib import Path
import json
import subprocess
import unittest
from playwright.async_api import async_playwright
from common.utils.browser_utils import get_chromium_executable_path

ROOT = Path(__file__).resolve().parents[2]


class VerificationPointerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scaled_image_accepts_click_and_drag_once(self):
        module = './src/pages/accounts/VerificationPointerSurface.tsx'
        entry = f'''import React from 'react'; import {{createRoot}} from 'react-dom/client';
import {{VerificationPointerSurface}} from {json.dumps(module)};
window.commands=[];
createRoot(document.getElementById('root')).render(<div style={{{{width:200}}}}>
<VerificationPointerSurface src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='300'%3E%3C/svg%3E"
 disabled={{false}} onCommand={{command=>window.commands.push(command)}} /></div>);'''
        build = "const esbuild=require('./node_modules/esbuild'); process.stdout.write(esbuild.buildSync({stdin:{contents:" + json.dumps(entry) + ",loader:'tsx',resolveDir:process.cwd()},bundle:true,write:false,format:'iife',jsx:'automatic'}).outputFiles[0].text)"
        compiled = subprocess.run(['node','-e',build],cwd=ROOT/'frontend',capture_output=True,text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True,args=['--no-sandbox'],executable_path=get_chromium_executable_path())
            try:
                page=await browser.new_page()
                await page.set_content('<style>img{width:100%}</style><div id="root"></div>')
                await page.add_script_tag(content=compiled.stdout)
                image=page.get_by_alt_text('验证浏览器，支持点击和拖动')
                await image.wait_for()
                await page.wait_for_function("document.querySelector('img').naturalWidth===400")
                box=await image.bounding_box()
                await page.mouse.click(box['x']+20,box['y']+15)
                self.assertEqual(await page.evaluate('commands'), [{'action':'click','x':40,'y':30}])
                await page.mouse.move(box['x']+10,box['y']+15)
                await page.mouse.down()
                await page.mouse.move(box['x']+90,box['y']+15,steps=3)
                await page.mouse.up()
                commands=await page.evaluate('commands')
                self.assertEqual(len(commands),2)
                self.assertEqual(commands[1]['action'],'drag')
                self.assertEqual(commands[1]['points'][0],{'x':20,'y':30})
                self.assertEqual(commands[1]['points'][-1],{'x':180,'y':30})
            finally:
                await browser.close()
