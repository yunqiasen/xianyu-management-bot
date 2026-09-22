import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from stdlib_loader import load

class PublishTests(unittest.IsolatedAsyncioTestCase):
    def setup_service(self, result=None, exception=None, existing=None):
        session=SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        @asynccontextmanager
        async def maker(): yield session
        logs=SimpleNamespace(get_by_request_id=AsyncMock(return_value=existing),
            create_log=AsyncMock(return_value=SimpleNamespace(id=7)),update_log=AsyncMock())
        address=SimpleNamespace(apply_to_item_data=lambda data: dict(data), to_log_fields=lambda: {})
        address_svc=SimpleNamespace(resolve_publish_address=AsyncMock(return_value=address))
        publish=AsyncMock(return_value=result, side_effect=exception)
        m=load('common/services/publish_execution_service.py', asyncio=asyncio,
            product_admission=load('common/services/product_admission.py').product_admission,
            PublishLogService=lambda session: logs, PublishAddressService=lambda session: address_svc,
            async_session_maker=maker, IntegrityError=RuntimeError,
            detect_publish_account_capability=AsyncMock(return_value={'success':True,'is_fish_shop':True}),
            ensure_publish_capability_reliable=lambda v:v, publish_single_item=publish)
        m.execute_single_publish.__globals__['_get_account']=AsyncMock(return_value=SimpleNamespace(cookie='fixture',account_id='a',status='active',metadata_json={}))
        m.execute_single_publish.__globals__['_sync_account_items_after_publish']=AsyncMock(return_value={})
        return m, session, logs, publish

    async def call(self,m,session):
        return await m.execute_single_publish(session,1,'a',{'title':'fixture','sku_rows':[]},publish_request_id='req-1')

    async def test_manual_timeout_is_unknown_not_failed(self):
        m,s,logs,pub=self.setup_service(exception=TimeoutError())
        result=await self.call(m,s)
        self.assertTrue(result.get('unknown')); self.assertEqual(logs.update_log.call_args.kwargs['status'],'unknown')

    async def test_manual_ambiguous_success_needs_identity(self):
        m,s,logs,pub=self.setup_service(result={'success':True})
        result=await self.call(m,s)
        self.assertTrue(result.get('unknown')); self.assertFalse(result['success'])

    async def test_unknown_replay_does_not_publish(self):
        existing=SimpleNamespace(id=7,status='unknown',item_id=None,item_url=None,user_id=1,account_id='a')
        m,s,logs,pub=self.setup_service(existing=existing,result={'success':True,'item_id':'x'})
        result=await self.call(m,s)
        self.assertTrue(result.get('unknown')); pub.assert_not_awaited()

    async def test_success_replay_does_not_publish(self):
        existing=SimpleNamespace(id=7,status='success',item_id='x',item_url=None,user_id=1,account_id='a')
        m,s,logs,pub=self.setup_service(existing=existing,result={'success':True,'item_id':'x'})
        result=await self.call(m,s)
        self.assertTrue(result.get('idempotent_reused')); pub.assert_not_awaited()

    async def test_request_id_other_owner_never_reveals_item(self):
        existing=SimpleNamespace(id=7,status='success',item_id='secret-item',item_url=None,user_id=2,account_id='other')
        m,s,logs,pub=self.setup_service(existing=existing,result={'success':True,'item_id':'x'})
        result=await self.call(m,s)
        self.assertFalse(result['success']); self.assertNotIn('item_id',result); pub.assert_not_awaited()

    async def test_publish_snapshot_saved_before_external_call(self):
        m,s,logs,pub=self.setup_service(result={'success':True,'item_id':'x'})
        await self.call(m,s)
        self.assertEqual(logs.create_log.call_args.kwargs.get('publish_snapshot'),{'title':'fixture','sku_rows':[]})

    async def test_request_collision_other_owner_never_reveals_item(self):
        m,s,logs,pub=self.setup_service(result={'success':True,'item_id':'x'})
        existing=SimpleNamespace(id=99,user_id=2,account_id='other',status='success',item_id='secret-item',item_url=None)
        logs.get_by_request_id.side_effect=[None,existing]
        logs.create_log.side_effect=RuntimeError('unique')
        result=await self.call(m,s)
        self.assertFalse(result['success']); self.assertNotIn('item_id',result); pub.assert_not_awaited()
