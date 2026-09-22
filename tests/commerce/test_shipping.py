"""真实确认服务仅发一次请求；HTTP层不吞并持久预算。"""
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

ROOT=Path(__file__).resolve().parents[2]/'websocket/app/services/shipping'
def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
base=load('commerce_shipping_base','base.py')
with patch.dict(sys.modules,{'app.services.shipping.base':base}):
    shipping=load('commerce_shipping_confirm','confirm_service.py')

class NetworkFailure:
    def __init__(self): self.calls=0
    def post(self,*args,**kwargs):self.calls+=1;return self
    async def __aenter__(self):raise TimeoutError('SECRET')
    async def __aexit__(self,*args):pass

class ShippingTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_ambiguity_returns_unknown_without_inline_retry(self):
        http=NetworkFailure();service=shipping.ConfirmShippingService(None,http,1)
        service._account=SimpleNamespace(account_id='a');service._cookies_str='fixture'
        service._get_token_from_cookies=lambda:'fixture'
        with patch.object(shipping.asyncio,'sleep',AsyncMock()):result=await service.auto_confirm('o')
        self.assertEqual(http.calls,1)
        self.assertEqual(result.get('outcome'),'unknown')
        self.assertNotIn('SECRET',str(result))
