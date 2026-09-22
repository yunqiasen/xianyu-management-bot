import importlib.util
from pathlib import Path
import sys
import unittest


class ConfigTests(unittest.TestCase):
    def gateway(self):
        path = Path(__file__).resolve().parents[2] / 'common/services/ai_gateway.py'
        self.assertTrue(path.exists(), '缺少统一 AI 调用入口')
        spec = importlib.util.spec_from_file_location('ai_config_under_test', path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_unknown_provider_is_rejected(self):
        gateway = self.gateway()
        with self.assertRaises(ValueError):
            gateway.AIConfig.from_settings({'provider_type': 'typo'})

    def test_urls_and_separate_protocols(self):
        gateway = self.gateway()
        for provider, root, expected in [
            ('openai_compatible', 'https://example.test', '/v1/chat/completions'),
            ('openai_compatible', 'https://example.test/proxy/v1/chat/completions', '/proxy/v1/chat/completions'),
            ('responses', 'https://example.test/v1', '/v1/responses'),
            ('responses', 'https://example.test/v1/responses', '/v1/responses'),
            ('anthropic', 'https://example.test/v1/messages', '/v1/messages'),
            ('gemini', 'https://example.test/v1beta/models/model:generateContent', '/v1beta/models/model:generateContent'),
            ('dashscope_app', 'https://example.test', '/api/v1/apps/app/completion'),
            ('azure', 'https://example.test', '/openai/deployments/deploy/chat/completions?api-version=2024-10-21'),
        ]:
            with self.subTest(provider=provider, root=root):
                config = gateway.AIConfig.from_settings(dict(provider_type=provider, base_url=root, api_key='fixture', model_name='model', app_id='app', azure_deployment='deploy', azure_api_version='2024-10-21'))
                self.assertEqual(config.url, 'https://example.test' + expected)
                self.assertEqual(config.provider_type, provider)
                self.assertNotIn('fixture', repr(config))

    def test_azure_requires_explicit_fields(self):
        gateway = self.gateway()
        with self.assertRaises(ValueError):
            gateway.AIConfig.from_settings(dict(provider_type='azure', base_url='https://example.test', api_key='fixture', model_name='model'))

    def test_url_credentials_and_placeholder_keys_rejected(self):
        gateway = self.gateway()
        for url, key in [('https://user:password@example.test', 'key'), ('https://example.test?key=secret', 'key'), ('https://example.test', '********'), ('https://example.test:99999', 'key'), ('https://example.test:word', 'key'), ('https://exa mple.test', 'key')]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                gateway.AIConfig.from_settings(dict(base_url=url, api_key=key, model_name='model'))
