"""Isolated UI test server. Never start against runtime/business data."""
import os
import sys
import time
from pathlib import Path

if os.environ.get('DB_PATH') != '/fixture/test.db':
    raise RuntimeError('Fixture server requires its isolated database')
sys.path.insert(0, '/app')
import reply_server
from db_manager import db_manager
from utils.item_publisher import ItemPublisher
sys.path.insert(0, '/app/tests')
from test_upstream_categories import recommendation
import uvicorn

reply_server.SESSION_TOKENS[os.environ['TEST_TOKEN']] = {
    'user_id': 1, 'username': 'admin', 'is_admin': True, 'timestamp': time.time(),
}
db_manager.save_cookie('fixture-account', 'unb=fixture; _m_h5_tk=fixture_1', user_id=1)
import asyncio
import cookie_manager
cookie_manager.manager = cookie_manager.CookieManager(asyncio.new_event_loop())
for i in range(25):
    db_manager.add_product_material(1, {
        'title': (f'素材{i:02d}-' + 'LongUnbrokenTitle' * 8), 'description': 'UI测试素材',
        'images': [{'url': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aCfkAAAAASUVORK5CYII='}],
    })
async def fake_mtop(self, *, api_name, payload, **kwargs):
    if api_name != 'mtop.taobao.idle.kgraph.property.recommend':
        raise RuntimeError('Unexpected platform write in UI fixture')
    response = recommendation()
    response['data']['cardList'].append({'cardData': {'propertyId':'tags','propertyName':'标签（多选）','isMultiple':'1', 'valuesList':[
        {'valueId':'t1','text':'标签一','transportData':{'valueId':'t1','valueName':'标签一'}},
        {'valueId':'t2','text':'标签二','transportData':{'valueId':'t2','valueName':'标签二'}},
    ]}})
    if '无默认分类' in str(payload):
        response['data'].pop('categoryPredictResult', None)
        for value in response['data']['cardList'][0]['cardData']['valuesList']:
            value['isClicked'] = '0'
    return response
ItemPublisher._post_mtop = fake_mtop
uvicorn.run(reply_server.app, host='0.0.0.0', port=8090)
