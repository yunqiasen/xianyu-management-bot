import json
from types import SimpleNamespace
import unittest
from test_settings import ROOT
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from fastapi import FastAPI
import httpx
from app.api.routes import ai as routes
from app.api import deps
from app.services.account_service import AccountService
from app.services.ai_reply_service import AIReplySettingsService
from common.models.xy_account import XYAccount
from common.models.ai_preset import AIPreset
from common.models.user import UserRole


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as conn:
            await conn.run_sync(XYAccount.__table__.create)
            await conn.run_sync(AIPreset.__table__.create)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.account = XYAccount(id=1, owner_id=7, account_id='account-a', cookie='fixture', login_method='manual', metadata_json={'ai_reply_settings': {'api_key': 'fixture-secret', 'base_url': 'https://example.test/v1', 'model_name': 'model'}})
        self.session.add(self.account)
        await self.session.commit()
        self.app = FastAPI()
        self.app.include_router(routes.router, prefix='/api/v1/ai-reply-settings')
        self.app.include_router(routes.test_router, prefix='/api/v1')
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: SimpleNamespace(id=7, role=UserRole.MEMBER)
        self.app.dependency_overrides[deps.get_account_service] = lambda: AccountService(self.session)
        self.app.dependency_overrides[deps.get_ai_reply_service] = lambda: AIReplySettingsService(self.session)
        self.app.dependency_overrides[deps.get_db_session] = lambda: self.session
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://fixture')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.session.close()
        await self.engine.dispose()

    async def test_get_and_list_never_return_keys(self):
        for path in ['', '/account-a']:
            result = await self.client.get('/api/v1/ai-reply-settings' + path)
            self.assertEqual(result.status_code, 200)
            self.assertNotIn('fixture-secret', result.text)
            self.assertIn('********', result.text)

    async def test_protocol_save_roundtrip_and_validation(self):
        result = await self.client.put('/api/v1/ai-reply-settings/account-a', json={'provider_type': 'responses', 'timeout_seconds': 12, 'api_key': '********', 'ai_enabled': True})
        self.assertTrue(result.json()['success'])
        saved = (await self.client.get('/api/v1/ai-reply-settings/account-a')).json()
        self.assertEqual(saved['timeout_seconds'], 12)
        bad = await self.client.put('/api/v1/ai-reply-settings/account-a', json={'provider_type': 'unknown'})
        self.assertFalse(bad.json()['success'])

    async def test_preset_crud_apply_via_actual_api(self):
        base = '/api/v1/ai-reply-settings/presets'
        created = await self.client.post(base, json={'name': '模板', 'source_account_id': 'account-a', 'settings': {'api_key': '********', 'provider_type': 'responses', 'ai_enabled': True}})
        self.assertEqual(created.status_code, 200)
        self.assertTrue(created.json()['success'])
        self.assertNotIn('fixture-secret', created.text)
        preset_id = created.json()['data']['id']
        updated = await self.client.put(f'{base}/{preset_id}', json={'name': '更新模板', 'settings': {'max_tokens': 200}})
        self.assertTrue(updated.json()['success'])
        batch = await self.client.post(f'{base}/{preset_id}/apply', json={'account_ids': ['account-a', 'missing']})
        self.assertFalse(batch.json()['success'])
        self.assertEqual([r['success'] for r in batch.json()['data']['results']], [True, False])
        self.assertTrue((await self.client.delete(f'{base}/{preset_id}')).json()['success'])
        self.assertEqual((await self.client.get(base)).json()['data'], [])
        self.assertEqual((await self.client.get('/api/v1/ai-reply-settings/account-a')).json()['max_tokens'], 200)

    async def test_bulk_reports_each_account(self):
        response = await self.client.put('/api/v1/ai-reply-settings', json={'account-a': {'max_tokens': 99}, 'missing': {'max_tokens': 88}})
        body = response.json()
        self.assertFalse(body['success'])
        self.assertEqual(len(body['data']['results']), 2)

    async def test_missing_timeout_uses_approved_sixty_seconds_without_rewriting_saved_value(self):
        url='/api/v1/ai-reply-settings/account-a'
        self.assertEqual((await self.client.get(url)).json()['timeout_seconds'],60)
        response=await self.client.put(url,json={'timeout_seconds':12})
        self.assertTrue(response.json()['success'])
        self.assertEqual((await self.client.get(url)).json()['timeout_seconds'],12)
