"""S1 管理HTTP: 独立app命名空间，命令见领域交付报告。"""
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import insert
import httpx
from fastapi import FastAPI


class ManagementApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(Path('backend-web/app/api/routes/reply_management.py').exists(), '缺少管理API')
        from app.api.routes import reply_management as api
        from common.models.reply_state import metadata, TABLES
        self.api = api
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + self.tmp.name + '/api.db')
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda sync: metadata.create_all(sync, tables=TABLES))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        from common.services.reply_state import ReplyState
        self.store = ReplyState(self.sessions)
        self.app = FastAPI()
        self.app.include_router(api.router)
        async def owned(account_id: str):
            if account_id != 'a':
                from fastapi import HTTPException
                raise HTTPException(404, '账号不存在')
            return SimpleNamespace(account_id='a', owner_id=1)
        self.app.dependency_overrides[api.owned_account] = owned
        self.app.dependency_overrides[api.get_reply_state] = lambda: self.store
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://fixture')

    async def asyncTearDown(self):
        if hasattr(self, 'client'):
            await self.client.aclose()
            await self.engine.dispose()
            self.tmp.cleanup()

    async def test_policy_versions_conflict_and_scope(self):
        res = await self.client.get('/reply-controls/a/policy')
        self.assertEqual(res.json()['data']['strategy'], 'ai_first')
        res = await self.client.put('/reply-controls/a/policy', json={'strategy':'legacy', 'version':0})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['data']['version'], 1)
        self.assertEqual((await self.client.put('/reply-controls/a/policy', json={'strategy':'ai_first','version':0})).status_code, 409)
        self.assertEqual((await self.client.get('/reply-controls/b/policy')).status_code, 404)

    async def test_exclusive_import_errors_and_export_round_trip(self):
        rows = [{'item_id':'i1','content':'hello'}, {'item_id':'','content':'bad'}, {'item_id':'i2','content':''}]
        res = await self.client.post('/reply-controls/a/exclusive/import', json={'rows':rows})
        self.assertEqual(res.json()['data']['saved'], 2)
        self.assertEqual(res.json()['data']['errors'][0]['row'], 2)
        exported = (await self.client.get('/reply-controls/a/exclusive')).json()['data']
        self.assertEqual(len(exported), 2)
        self.assertEqual(exported[1]['content'], '')
        self.assertIsNotNone(await self.store.exclusive('a', 'i2'))

    async def test_filter_rules_are_live_and_invalid_regex_rejected(self):
        rule = {'pattern':'bad','match_mode':'exact','source':'ai','actions':['notify'],'item_id':''}
        res = await self.client.post('/reply-controls/a/filters', json=rule)
        self.assertEqual(res.status_code, 200)
        rule_id = res.json()['data']['id']
        self.assertEqual(len(await self.store.filters('a')), 1)
        res = await self.client.put('/reply-controls/a/filters/'+rule_id, json={**rule, 'enabled':False,'version':1})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(await self.store.filters('a'), [])
        self.assertEqual((await self.client.post('/reply-controls/a/filters', json={**rule,'pattern':'(a+)+$','match_mode':'regex'})).status_code, 422)

    async def test_existing_manual_send_api_persists_once_and_unknown(self):
        from app.api.routes import chat_new
        from unittest.mock import patch, AsyncMock
        from common.models.xy_account import XYAccount
        user = SimpleNamespace(id=1, role='user')
        account = SimpleNamespace(account_id='a', pause_duration=0)
        client = SimpleNamespace(myid='seller', is_connected=True, send_text_message=AsyncMock(side_effect=TimeoutError()))
        request = chat_new.SendMessageRequest(cid='c', toUserId='buyer', text='hello', requestId='same')
        with patch.object(chat_new, '_get_owned_chat_account', AsyncMock(return_value=account)), \
             patch.object(chat_new, 'get_im_session_manager', return_value=SimpleNamespace(clients={'a':client})), \
             patch.object(chat_new, 'ReplyState', return_value=self.store):
            first = await chat_new.send_message('a', request, current_user=user, db=None)
            second = await chat_new.send_message('a', request, current_user=user, db=None)
        self.assertEqual(first.data['status'], 'unknown')
        self.assertEqual(second.data['status'], 'unknown')
        client.send_text_message.assert_awaited_once()
        self.assertEqual(await self.store.pause_remaining('a', 'c'), 0)

    async def test_avatar_cache_is_partitioned_by_account(self):
        from app.services.chat_new import avatar_service
        from unittest.mock import AsyncMock, patch
        redis = SimpleNamespace(get=AsyncMock(return_value='{"avatar":"x","nick":"buyer"}'))
        with patch.object(avatar_service, 'get_redis_client', AsyncMock(return_value=redis)):
            await avatar_service.get_user_info('a', 'same', 'fixture', None)
            await avatar_service.get_user_info('b', 'same', 'fixture', None)
        keys = [call.args[0] for call in redis.get.await_args_list]
        self.assertNotEqual(keys[0], keys[1])

    async def test_platform_explicit_rejection_is_failed_not_unknown(self):
        from app.services.chat_new.im_client import GoofishImClient
        async def transport():
            GoofishImClient._raise_if_send_rejected({'code':200, 'body':{'reason':'fixture reject'}})
        result = await self.store.send('a', 'c', 'reject', 'hello', transport)
        self.assertEqual(result['status'], 'failed')

    async def test_unknown_verification_api_never_accepts_claimed_status_or_resends(self):
        from unittest.mock import AsyncMock
        send = AsyncMock(return_value={'status':'unknown', 'messageId':'platform-1'})
        await self.store.send('a', 'c', 'request-1', 'hello', send)
        path = '/reply-controls/a/outbound/c/request-1/verify'
        response = await self.client.post(path, json={'status':'confirmed'})
        self.assertEqual(response.status_code, 422, '核实接口缺失或允许客户端自报成功')
        self.assertTrue(hasattr(self.api, 'get_platform_history_evidence'))
        fetch = AsyncMock(return_value=[{'messageId':'other','isSelf':True,'text':'hello','type':'text'}])
        self.app.dependency_overrides[self.api.get_platform_history_evidence] = lambda: fetch
        response = await self.client.post(path, json={})
        self.assertEqual(response.json()['data']['status'], 'unknown')
        fetch.return_value = [{'messageId':'platform-1','isSelf':True,'text':'hello','type':'text'}]
        response = await self.client.post(path, json={})
        self.assertEqual(response.json()['data']['status'], 'confirmed')
        self.assertEqual(response.json()['data']['evidence']['source'], 'platform_history')
        self.assertEqual((await self.client.post(path.replace('/a/', '/b/'), json={})).status_code, 404)
        send.assert_awaited_once()

    async def test_global_filter_applies_to_future_owned_accounts_and_pause_duration_roundtrips(self):
        from common.models.xy_account import XYAccount
        async with self.engine.begin() as connection:
            await connection.run_sync(XYAccount.__table__.create)
        async with self.sessions() as session:
            session.add_all([XYAccount(id=1,owner_id=1,account_id='a',cookie='fixture',login_method='manual'),
                             XYAccount(id=2,owner_id=2,account_id='other',cookie='fixture',login_method='manual')])
            await session.commit()
        rule={'pattern':'人工','source':'user','actions':['pause'],'all_accounts':True,'pause_minutes':7}
        response=await self.client.post('/reply-controls/a/filters',json=rule)
        self.assertEqual(response.status_code,200,response.text)
        rule_id=response.json()['data']['id']
        listed=(await self.client.get('/reply-controls/a/filters')).json()['data'][0]
        self.assertTrue(listed.get('all_accounts'))
        self.assertEqual(listed.get('pause_minutes'),7)
        async with self.sessions() as session:
            session.add(XYAccount(id=3,owner_id=1,account_id='future',cookie='fixture',login_method='manual'))
            await session.commit()
        self.assertEqual(len(await self.store.filters('future')),1)
        self.assertEqual(await self.store.filters('other'),[])
        result=await self.store.filter_decision('future','chat','需要人工','user')
        self.assertTrue(result.blocks_reply)
        self.assertGreater(await self.store.pause_remaining('future','chat'),410)
        self.assertEqual(await self.store.pause_remaining('other','chat'),0)
        await self.client.put('/reply-controls/a/filters/'+rule_id,json={**rule,'version':1,'enabled':False})
        self.assertEqual(await self.store.filters('future'),[])

if __name__ == '__main__': unittest.main()
