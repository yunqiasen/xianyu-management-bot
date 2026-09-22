"""S1 黑名单HTTP/聊天按钮一致性；S2为可观测平台替身。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from sqlalchemy import insert, select
import api_suite

class BlacklistTests(api_suite.ManagementApiTests):
    async def test_chat_action_and_query_update_same_owner_scoped_platform_list(self):
        from app.api.routes import chat_new
        from app.services.blacklist_service import BlacklistService
        from common.models.xy_platform_blacklist import XYPlatformBlacklist
        from common.models.user import User
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda c: self.api.reply_events.metadata.create_all(c, tables=[XYPlatformBlacklist.__table__, User.__table__]))
        account = SimpleNamespace(account_id='a', owner_id=1, cookie='fixture')
        user = SimpleNamespace(id=1, role='user')
        async with self.sessions() as db:
            with patch.object(chat_new, '_get_owned_chat_account', AsyncMock(return_value=account)), \
                 patch.object(chat_new, 'official_blacklist_request', AsyncMock(return_value={'isInBlack':True})):
                self.assertIn('buyer_id', __import__('inspect').signature(chat_new.change_official_blacklist).parameters)
                result = await chat_new.change_official_blacklist('a', 'c', 'add', buyer_id='buyer', current_user=user, db=db)
                self.assertTrue(result.success)
                rows, total = await BlacklistService(db).list_platform(owner_id=1)
                self.assertEqual(total, 1)
                self.assertEqual(rows[0]['buyer_id'], 'buyer')
                rows, total = await BlacklistService(db).list_platform(owner_id=2)
                self.assertEqual(total, 0)
                result = await chat_new.change_official_blacklist('a', 'c', 'remove', buyer_id='buyer', current_user=user, db=db)
                self.assertTrue(result.success)
                self.assertEqual((await BlacklistService(db).list_platform(owner_id=1))[1], 0)

    async def test_sync_known_conversations_keeps_unverified_members(self):
        from common.models.xy_platform_blacklist import XYPlatformBlacklist
        from common.models.user import User
        from app.services.blacklist_service import BlacklistService
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda c: self.api.reply_events.metadata.create_all(c, tables=[XYPlatformBlacklist.__table__, User.__table__]))
        await self.store.record_message('a','c1','e1','user','hi','buyer1')
        await self.store.record_message('a','c2','e2','user','hi','buyer2')
        self.assertTrue(hasattr(self.api, 'get_official_blacklist_evidence'), '缺少平台名单同步入口')
        fetch = AsyncMock(side_effect=[True, TimeoutError()])
        self.app.dependency_overrides[self.api.get_official_blacklist_evidence] = lambda: fetch
        response = await self.client.post('/reply-controls/a/platform-blacklist/sync', json={})
        data = response.json()['data']
        self.assertEqual(data['synced'], 1)
        self.assertEqual(data['errors'][0]['chat_id'], 'c2')
        async with self.sessions() as db:
            rows, total = await BlacklistService(db).list_platform(owner_id=1)
            self.assertEqual([r['buyer_id'] for r in rows], ['buyer1'])
        self.assertEqual((await self.client.post('/reply-controls/b/platform-blacklist/sync', json={})).status_code, 404)

    async def test_platform_sync_has_no_unfenced_or_unbound_http_fallback(self):
        from app.services.chat_new import official_blacklist_service as service
        self.assertIn('account', __import__('inspect').signature(service.official_blacklist_request).parameters)
        account = SimpleNamespace(account_id='a', proxy_type='http', proxy_host='fixed-proxy', proxy_port=8080,
            proxy_user=None, proxy_pass=None)
        from common.services.account_dispatch import DispatchError
        with patch.object(service, 'before_platform_request', AsyncMock(side_effect=DispatchError('missing_execution_context'))), \
             patch.object(service.httpx, 'AsyncClient') as http:
            with self.assertRaises(DispatchError):
                await service.official_blacklist_request('fixture','c','query', account=account)
            http.assert_not_called()
        with patch.object(service, 'before_platform_request', AsyncMock()), patch.object(service.httpx, 'AsyncClient') as http:
            account.proxy_port = 0
            with self.assertRaises(ValueError):
                await service.official_blacklist_request('fixture','c','query', account=account)
            http.assert_not_called()

if __name__ == '__main__': unittest.main()
