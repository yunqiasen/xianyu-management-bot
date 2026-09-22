"""真实 Chromium→账号现有 AI 面板→诊断 API；测试启动的子进程单独清理。"""
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
env = dict(os.environ, PYTHONPATH=f'{ROOT}:{ROOT}/backend-web:{ROOT}/tests/bargaining:{ROOT}/tests/ai',
           SQL_ECHO='false', AI_FIXTURE_PORT=str(api_port), AI_UI_PORT=str(ui_port))
processes = []
with open('/tmp/xymb-bargaining-browser-servers.log', 'w') as logs:
    try:
        processes.append(subprocess.Popen([str(ROOT / '.venv/bin/python'), '-m', 'uvicorn', 'browser_fixture:app',
            '--host', '127.0.0.1', '--port', str(api_port)], cwd=ROOT, env=env, stdout=logs, stderr=logs))
        processes.append(subprocess.Popen(['node', str(ROOT / 'frontend/node_modules/vite/bin/vite.js'),
            '--config', 'tests/ai/vite.config.mjs'], cwd=ROOT, env=env, stdout=logs, stderr=logs))
        for url in [f'http://127.0.0.1:{api_port}/openapi.json', f'http://127.0.0.1:{ui_port}/tests/bargaining/ui.html']:
            for _ in range(100):
                try:
                    urllib.request.urlopen(url, timeout=1).close()
                    break
                except Exception:
                    time.sleep(.15)
            else:
                raise AssertionError(f'fixture 未就绪：{url}')
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=str(sorted((Path.home() / '.cache/ms-playwright').glob('chromium-*/chrome-*/chrome'))[-1]), args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1100, 'height': 1300})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{ui_port}/tests/bargaining/ui.html')
            section = page.get_by_label('议价与历史候选诊断')
            section.wait_for()
            text = section.inner_text()
            assert '已确认议价 1 次' in text, text
            assert '人工消息' in text and 'latest-buyer' in text, text
            assert 'DEV43' in text and '裁剪 0 条' not in text, text
            page.get_by_role('button', name='刷新诊断', exact=True).click()
            section.wait_for()
            assert '已确认议价 1 次' in section.inner_text()
            assert not errors, errors
            browser.close()
        print('PASS Chromium：实际回复诊断→现有 AI 面板展示次数/历史范围/裁剪→刷新')
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
