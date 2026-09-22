import importlib
import json
import unittest
from test_settings import ROOT
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from common.models.xy_account import XYAccount


class PresetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue((ROOT / 'backend-web/app/services/ai_preset_service.py').exists(), '缺少预设业务服务')
        self.module = importlib.import_module('app.services.ai_preset_service')
        self.model = importlib.import_module('common.models.ai_preset').AIPreset
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as conn:
            await conn.run_sync(XYAccount.__table__.create)
            await conn.run_sync(self.model.__table__.create)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        for pk, owner in [(1, 7), (2, 7), (3, 8)]:
            self.session.add(XYAccount(id=pk, owner_id=owner, account_id=f'account-{pk}', cookie='fixture', login_method='manual', metadata_json={'keep': pk}))
        await self.session.commit()
        self.service = self.module.AIPresetService(self.session)
        self.settings = {'provider_type': 'responses', 'api_key': 'fixture-secret', 'model_name': 'model', 'base_url': 'https://example.test/v1', 'ai_enabled': True}

    async def asyncTearDown(self):
        if hasattr(self, 'session'):
            await self.session.close()
            await self.engine.dispose()

    async def test_crud_owner_scope_and_mask_retention(self):
        preset = await self.service.create(7, '我的预设', self.settings)
        self.assertEqual(preset['settings']['api_key'], '********')
        self.assertNotIn('fixture-secret', json.dumps(await self.service.list(7)))
        self.assertEqual(await self.service.list(8), [])
        with self.assertRaises(ValueError):
            await self.service.update(8, preset['id'], name='other')
        await self.service.update(7, preset['id'], name='新名字', settings={'api_key': '********', 'max_tokens': 100})
        loaded = (await self.service.list(7))[0]
        self.assertEqual(loaded['name'], '新名字')
        self.assertEqual(loaded['settings']['max_tokens'], 100)
        with self.assertRaises(ValueError):
            await self.service.delete(8, preset['id'])
        await self.service.delete(7, preset['id'])
        self.assertEqual(await self.service.list(7), [])

    async def test_batch_snapshot_partial_failures_delete_is_independent(self):
        preset = await self.service.create(7, '批量', self.settings)
        results = await self.service.apply(7, preset['id'], ['account-1', 'account-3', 'missing', 'account-2'])
        self.assertEqual([row['success'] for row in results], [True, False, False, True])
        await self.service.delete(7, preset['id'])
        for pk in (1, 2):
            account = await self.session.get(XYAccount, pk)
            self.assertEqual(account.metadata_json['keep'], pk)
            self.assertEqual(account.metadata_json['ai_reply_settings']['api_key'], 'fixture-secret')
        other = await self.session.get(XYAccount, 3)
        self.assertEqual(other.metadata_json, {'keep': 3})

    async def test_mask_without_source_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.service.create(7, 'missing key', self.settings | {'api_key': '********'})

    async def test_foreign_preset_cannot_be_applied(self):
        preset = await self.service.create(7, '我的', self.settings)
        with self.assertRaises(ValueError):
            await self.service.apply(8, preset['id'], ['account-3'])

    async def test_empty_secret_snapshot_clears_old_target_secret(self):
        from app.services.ai_reply_service import AIReplySettingsService
        account = await self.session.get(XYAccount, 1)
        await AIReplySettingsService(self.session).update_settings(account, self.settings)
        preset = await self.service.create(7, '停用无密钥', self.settings | {'ai_enabled': False, 'api_key': ''})
        await self.service.apply(7, preset['id'], ['account-1'])
        await self.session.refresh(account)
        self.assertEqual(account.metadata_json['ai_reply_settings']['api_key'], '')
