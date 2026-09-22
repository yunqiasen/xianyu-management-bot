"""真实 Chromium→AI 配置组件→真实 FastAPI/SQLite→本地模型。自动清理自建进程。"""
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]

def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

api_port, ui_port = free_port(), free_port()
env = dict(os.environ, PYTHONPATH=f'{ROOT}:{ROOT}/backend-web:{ROOT}/tests/ai', SQL_ECHO='false', AI_FIXTURE_PORT=str(api_port), AI_UI_PORT=str(ui_port))
logs = open('/tmp/xymb-ai-browser-servers.log', 'w')
processes = []
css_file = ROOT / 'tests/ai/.fixture.css'
try:
    subprocess.run([str(ROOT / 'frontend/node_modules/.bin/tailwindcss'), '-i', 'tests/ai/ui.css', '-o', str(css_file), '--content', 'frontend/src/pages/accounts/AISettingsPanel.tsx'], cwd=ROOT, check=True, stdout=logs, stderr=logs)
    processes.append(subprocess.Popen([str(ROOT / '.venv/bin/python'), '-m', 'uvicorn', 'browser_fixture:app', '--host', '127.0.0.1', '--port', str(api_port)], cwd=ROOT, env=env, stdout=logs, stderr=logs))
    processes.append(subprocess.Popen(['node', str(ROOT / 'frontend/node_modules/vite/bin/vite.js'), '--config', 'tests/ai/vite.config.mjs'], cwd=ROOT, env=env, stdout=logs, stderr=logs))
    for url in [f'http://127.0.0.1:{api_port}/openapi.json', f'http://127.0.0.1:{ui_port}/tests/ai/ui.html']:
        for _ in range(80):
            try:
                urllib.request.urlopen(url, timeout=1).close()
                break
            except Exception:
                time.sleep(.2)
        else:
            raise AssertionError(f'fixture 未就绪: {url}')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=str(sorted((Path.home() / '.cache/ms-playwright').glob('chromium-*/chrome-*/chrome'))[-1]), args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1200, 'height': 1600})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'http://127.0.0.1:{ui_port}/tests/ai/ui.html')
        page.get_by_label('协议', exact=True).wait_for()
        assert page.get_by_label('API Key', exact=True).input_value() == '********'
        page.get_by_label('协议', exact=True).select_option('responses')
        page.get_by_label('API 地址', exact=True).fill(f'http://127.0.0.1:{api_port}/fixture/v1')
        page.get_by_role('button', name='保存并测试', exact=True).click()
        page.get_by_text('本地 Responses 测试成功', exact=True).wait_for()
        page.get_by_label('预设名称').fill('浏览器模板')
        page.get_by_role('button', name='另存新预设', exact=True).click()
        page.get_by_text('当前配置已另存为预设', exact=True).wait_for()
        page.get_by_label('account-2', exact=True).check()
        page.get_by_label('missing', exact=True).check()
        page.get_by_role('button', name='批量应用（3）', exact=True).click()
        page.get_by_text('已应用 2/3 个账号', exact=True).wait_for()
        assert 'missing · 失败' in page.get_by_label('逐账号应用结果').inner_text()
        page.get_by_label('预设名称').fill('已改名模板')
        page.get_by_role('button', name='重命名', exact=True).click()
        page.get_by_text('预设已重命名', exact=True).wait_for()
        page.get_by_role('button', name='删除预设', exact=True).click()
        page.get_by_text('预设已删除，已应用账号保持原配置', exact=True).wait_for()
        page.get_by_role('button', name='保存并测试', exact=True).click()
        page.get_by_text('本地 Responses 测试成功', exact=True).wait_for()
        assert not errors, errors
        page.screenshot(path=str(Path(os.environ.get('XYMB_VERIFY_OUTPUT', '/tmp')) / 'xymb-ai-browser-smoke.png'), full_page=True)
        browser.close()
    print('PASS Chromium：配置保存→Responses 测试→预设新建/改名/批量部分失败/删除→账号副本继续测试成功')
finally:
    for process in reversed(processes):
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    logs.close()
    css_file.unlink(missing_ok=True)
