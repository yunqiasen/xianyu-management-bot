"""S1 real React component browser check, existing local Chromium/Vite only."""
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_reliability_panel_gate_baseline_error_notification_and_region():
    from playwright.sync_api import sync_playwright
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    log = open('/tmp/xymb-monitor-ui-vite.log', 'w')
    server = subprocess.Popen([str(ROOT/'frontend/node_modules/.bin/vite'), '--host', '127.0.0.1',
                              '--port', str(port), '--strictPort', '--config', str(ROOT/'tests/monitor/vite.fixture.config.mts')], cwd=ROOT/'frontend', stdout=log, stderr=log)
    url = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            try:
                urlopen(url, timeout=.2).close(); break
            except Exception: time.sleep(.1)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=str(sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-*/chrome'))[-1]))
            page = browser.new_page()
            page.on('pageerror', lambda error: print('BROWSER:', error))
            state = {'enabled':False, 'release_gate':'DEV43', 'baseline_ready':False,
                     'phase':'disabled', 'generation':0, 'region':'', 'pages':[], 'events':[], 'items':[]}
            writes = []
            def api(route):
                if route.request.method == 'PUT':
                    data = route.request.post_data_json; writes.append(data); state['region'] = data['region']
                route.fulfill(content_type='application/json', body=json.dumps({'success':True,'data':state}))
            page.route('**/api/v1/product-monitor/listing-tasks/1/reliability', api)
            html = '''<html><body><div id="root"></div><script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$=()=>{}; window.$RefreshSig$=()=>(type)=>type;
window.__vite_plugin_react_preamble_installed__=true;
await import('/@fs/''' + str(ROOT/'tests/monitor/ui-fixture.tsx') + "');</script></body></html>"
            page.route('**/monitor-fixture', lambda r: r.fulfill(content_type='text/html', body=html))
            page.goto(url+'/monitor-fixture')
            page.get_by_text('DEV43覆盖验收门保留', exact=False).wait_for(timeout=12000)
            assert page.get_by_label('监控地区').count() == 0
            state.update(enabled=True, phase='partial', generation=1, expected_pages=2,
                         pages=[{'page':1,'status':'complete'}, {'page':2,'status':'http_error'}])
            page.get_by_role('button', name='刷新可靠性').click()
            page.get_by_text('基线未完成，不推新增', exact=True).wait_for()
            page.get_by_text('HTTP错误', exact=False).wait_for()
            page.get_by_label('监控地区').fill('杭州')
            page.get_by_role('button', name='保存地区').click()
            page.wait_for_function("document.querySelector('[aria-label=监控地区]').value === '杭州'")
            assert writes == [{'region':'杭州'}]
            state.update(baseline_ready=True, phase='partial', generation=2, events=[{'id':'ev','kind':'price_drop',
                'item_id':'123','summary':'相机降价', 'price':'80', 'old_price':'100', 'enqueue_status':'queued',
                'notification_event_id':'n','deliveries':[{'channel_id':1,'status':'unknown','attempts':1}]}])
            page.get_by_role('button', name='刷新可靠性').click()
            page.get_by_text('待核实，不自动重发', exact=False).wait_for()
            page.screenshot(path=str(Path(os.environ.get('XYMB_VERIFY_OUTPUT', '/tmp')) / 'xymb-monitor-ui.png'), full_page=True)
            browser.close()
    finally:
        server.terminate(); server.wait(timeout=10); log.close()
