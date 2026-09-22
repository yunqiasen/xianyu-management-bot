import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from stdlib_loader import load

m = load('common/services/item_service.py', product_admission=load('common/services/product_admission.py').product_admission, asyncio=asyncio, product_error_status=load('common/services/product_results.py').product_error_status)

class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def run_pages(self, pages, page_size=2):
        svc = m.ItemService(SimpleNamespace(rollback=AsyncMock()))
        account = SimpleNamespace(account_id='a', owner_id=1, id=1, status='active', metadata_json={})
        manager = SimpleNamespace(get_item_list_info=AsyncMock(side_effect=pages), close=AsyncMock())
        svc._resolve_account_fetch_user_id=lambda a: 'a'
        svc._resolve_item_fetch_manager=AsyncMock(return_value=manager)
        svc._get_existing_item_map=AsyncMock(return_value={})
        saved=[]
        async def save(a, items): saved.extend(items); return len(items), len(items)
        svc.save_fetched_items=save
        with patch('asyncio.sleep', new=AsyncMock()):
            result = await svc._fetch_all_items_from_account_impl(account, page_size, max_pages=4)
        return result, saved, manager

    async def test_pages_deduplicated(self):
        r, saved, manager = await self.run_pages([
            {'success':True,'items':[{'id':'1'},{'id':'2'}]},
            {'success':True,'items':[{'id':'2'},{'id':'3'}]},
            {'success':True,'items':[]}])
        self.assertEqual([x['id'] for x in r['items']], ['1','2','3'])
        self.assertEqual(r['total_count'], 3)
        self.assertEqual([x['id'] for x in saved], ['1','2','3'])
        manager.close.assert_awaited_once()

    async def test_partial_failure_returns_confirmed_items(self):
        r, saved, _ = await self.run_pages([
            {'success':True,'items':[{'id':'1'},{'id':'2'}]},
            {'success':False,'message':'FAIL_SYS_TRAFFIC_LIMIT'}])
        self.assertFalse(r['success']); self.assertEqual(r.get('saved_count'),2)
        self.assertEqual(r.get('status'), 'rate_limited'); self.assertEqual(r.get('failed_page'),2)
        self.assertTrue(r.get('partial')); self.assertEqual(len(r.get('items',[])),2)

    async def test_missing_items_is_schema_error_not_empty(self):
        r, _, _ = await self.run_pages([{'success':True}])
        self.assertFalse(r['success']); self.assertEqual(r.get('status'), 'schema_error')

    async def test_explicit_end_of_pages_honored(self):
        r, _, manager = await self.run_pages([{'success':True,'items':[{'id':'1'},{'id':'2'}],'has_more':False}])
        self.assertTrue(r['success']); self.assertEqual(manager.get_item_list_info.await_count,1)

    async def test_invalid_page_size_rejected_before_network(self):
        r, _, manager = await self.run_pages([], page_size=0)
        self.assertFalse(r['success']); manager.get_item_list_info.assert_not_awaited()

    async def test_redis_lock_failure_never_falls_back_to_unlocked_fetch(self):
        svc=m.ItemService(SimpleNamespace(rollback=AsyncMock()))
        svc._fetch_all_items_from_account_impl=AsyncMock(return_value={'success':True})
        account=SimpleNamespace(account_id='a1')
        from unittest.mock import Mock
        with patch.dict(m.ItemService.fetch_all_items_from_account.__globals__,{'distributed_lock':Mock(side_effect=OSError('redis down'))}):
            result=await svc.fetch_all_items_from_account(account)
        self.assertFalse(result['success']); svc._fetch_all_items_from_account_impl.assert_not_awaited()

    async def test_single_page_missing_list_is_not_empty_success(self):
        svc=m.ItemService(SimpleNamespace(rollback=AsyncMock()))
        account=SimpleNamespace(account_id='a',status='active',metadata_json={})
        manager=SimpleNamespace(get_item_list_info=AsyncMock(return_value={'success':True}),close=AsyncMock())
        svc._resolve_account_fetch_user_id=lambda a:'a'
        svc._resolve_item_fetch_manager=AsyncMock(return_value=manager)
        svc.save_fetched_items=AsyncMock(return_value=(0,0))
        result=await svc.fetch_items_page_from_account(account)
        self.assertFalse(result['success'])
        self.assertEqual(result['status'],'schema_error')
        svc.save_fetched_items.assert_not_awaited()

    async def test_failure_retry_hint_distinguishes_limit_from_credentials(self):
        rate,_,_=await self.run_pages([{'success':False,'message':'TRAFFIC_LIMIT','retry_after':125}])
        auth,_,_=await self.run_pages([{'success':False,'message':'TOKEN_EXPIRED'}])
        self.assertEqual(rate.get('retry_after'),125)
        self.assertIsNone(auth.get('retry_after'))
        self.assertEqual(auth.get('retry_action'),'restore_session')
