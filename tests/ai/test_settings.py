"""S1 设置业务 + S3 内存 SQLite，仅创建本测试表。"""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend-web'))
os.environ['SQL_ECHO'] = 'false'
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from common.models.xy_account import XYAccount
from app.services.ai_reply_service import AIReplySettingsService


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as conn:
            await conn.run_sync(XYAccount.__table__.create)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.account = XYAccount(id=1, owner_id=7, account_id='account-a', cookie='fixture-cookie', login_method='manual', metadata_json={'other': {'keep': True}, 'ai_reply_settings': {'api_key': 'fixture-secret', 'model_name': 'model', 'base_url': 'https://example.test/v1', 'provider_type': 'openai_compatible'}})
        self.session.add(self.account)
        await self.session.commit()
        self.service = AIReplySettingsService(self.session)

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_mask_and_blank_keep_existing_secret(self):
        for key in ['********', '', '   ', None]:
            with self.subTest(key=key):
                await self.service.update_settings(self.account, {'api_key': key, 'ai_enabled': True})
                self.assertEqual((await self.service.get_settings(self.account))['api_key'], 'fixture-secret')
        self.assertEqual(self.account.metadata_json['other'], {'keep': True})

    async def test_unknown_provider_rejected_even_disabled(self):
        with self.assertRaises(ValueError):
            await self.service.update_settings(self.account, {'provider_type': 'unknown', 'ai_enabled': False})

    async def test_extended_protocol_fields_roundtrip_and_version(self):
        fields = {'provider_type': 'azure', 'azure_deployment': 'deploy', 'azure_api_version': '2024-10-21', 'azure_auth_mode': 'bearer', 'timeout_seconds': 17, 'max_tokens': 321, 'temperature': .2, 'max_context_chars': 1000, 'ai_enabled': True}
        await self.service.update_settings(self.account, fields)
        result = await self.service.get_settings(self.account)
        for key, value in fields.items():
            self.assertEqual(result.get(key), value, key)
        self.assertEqual(result.get('config_version'), 1)
        with self.assertRaises(ValueError):
            await self.service.update_settings(self.account, {'config_version': 0, 'model_name': 'stale'})

    async def test_explicit_clear_secret_requires_disabled_ai(self):
        await self.service.update_settings(self.account, {'ai_enabled': True})
        with self.assertRaises(ValueError):
            await self.service.update_settings(self.account, {'clear_api_key': True})
        await self.service.update_settings(self.account, {'ai_enabled': False, 'clear_api_key': True})
        self.assertEqual((await self.service.get_settings(self.account))['api_key'], '')

    async def test_invalid_protocol_config_is_rejected_on_save(self):
        for payload in [{'provider_type': 'azure'}, {'timeout_seconds': -1}, {'base_url': 'https://u:p@example.test'}, {'provider_type': 'responses', 'base_url': 'https://example.test/v1/chat/completions'}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                await self.service.update_settings(self.account, payload)

    async def test_invalid_prompt_json_rejected(self):
        for prompt in ['{"default":', '["wrong"]', '{"default": 123}']:
            with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                await self.service.update_settings(self.account, {'custom_prompts': prompt})
