"""S2：真实连接构造入口，外部客户端边界替身，无平台连接。"""
import ast
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import inspect
import random
import sys
import importlib.util
import aiohttp
from loguru import logger
from test_policy import p
ROOT = Path(__file__).parents[2]


def load_class(relative, name, **namespace):
    path = ROOT / relative
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    ns = dict(asyncio=asyncio, time=time, Optional=object, aiohttp=aiohttp, logger=logger, account_policy=p, **namespace)
    code = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), str(path), 'exec'), ns)
    return ns[name]


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def xianyu(self, config):
        cls = load_class('websocket/app/services/xianyu/xianyu_async.py', 'XianyuAsync')
        x = object.__new__(cls); x.proxy_config = config; x.cookie_id = 'fixture'
        return x

    async def test_http_missing_proxy_dependency_stops_instead_of_direct(self):
        x = self.xianyu(dict(proxy_type='socks5', proxy_host='127.0.0.1', proxy_port=9))
        with patch.dict(sys.modules, {'aiohttp_socks': None}):
            with self.assertRaises(Exception): x._build_session_connector()

    async def test_invalid_proxy_does_not_become_direct(self):
        x = self.xianyu(dict(proxy_type='http', proxy_host='', proxy_port=9))
        with self.assertRaises(ValueError): x._get_proxy_url()

    async def test_websocket_missing_proxy_dependency_never_dials_direct(self):
        connect = Mock()
        ws = SimpleNamespace(connect=connect, __version__='15')
        cls = load_class('websocket/app/services/xianyu/connection_manager.py', 'ConnectionManager',
                         websockets=ws, inspect=inspect, random=random)
        x = self.xianyu(dict(proxy_type='socks5', proxy_host='127.0.0.1', proxy_port=9))
        x.base_url = 'wss://fixture.invalid/ws'
        manager = object.__new__(cls); manager.xianyu=x; manager.cookie_id='fixture'
        with patch.dict(sys.modules, {'python_socks': None}):
            with self.assertRaises(Exception): await manager.create_websocket_connection({})
        connect.assert_not_called()

    async def test_websocket_explicit_direct_ignores_environment_proxy(self):
        def connect(uri, *, proxy=True, **kwargs): return dict(proxy=proxy, **kwargs)
        ws = SimpleNamespace(connect=connect, __version__='15')
        cls = load_class('websocket/app/services/xianyu/connection_manager.py', 'ConnectionManager',
                         websockets=ws, inspect=inspect, random=random)
        manager=object.__new__(cls); manager.cookie_id='fixture'
        manager.xianyu=self.xianyu({'proxy_type':'none'}); manager.xianyu.base_url='wss://fixture.invalid/ws'
        result = await manager.create_websocket_connection({})
        self.assertIsNone(result['proxy'])

    async def test_http_session_trace_checks_runtime_before_request(self):
        x=self.xianyu({'proxy_type':'none'})
        x._account_runtime=SimpleNamespace(check=AsyncMock())
        await x._check_account_execution(None, None, None)
        x._account_runtime.check.assert_awaited_once()

    async def test_token_local_request_receives_account_proxy(self):
        from common.services import im_token_api
        local=AsyncMock()
        config={'proxy_type':'http','proxy_host':'127.0.0.1','proxy_port':8123}
        with patch.object(im_token_api,'request_im_token',local):
            await im_token_api.request_im_token_with_fallback('unb=101','device',proxy_config=config)
        self.assertEqual(local.await_args.kwargs['proxy_config'],config)
