import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]

class IntegrationContractTests(unittest.TestCase):
    def test_live_engine_and_test_endpoint_use_shared_gateway(self):
        engine = (ROOT / 'websocket/app/services/xianyu/ai_reply_engine.py').read_text(encoding='utf-8-sig')
        service = (ROOT / 'common/services/ai_provider_service.py').read_text(encoding='utf-8-sig')
        route = (ROOT / 'backend-web/app/api/routes/ai.py').read_text(encoding='utf-8-sig')
        self.assertIn('generate_text', engine)
        self.assertIn('generate_text', service)
        self.assertIn('settings=settings', route)
        tree = ast.parse(engine)
        generate = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'generate_reply')
        calls = [n.func.attr for n in ast.walk(generate) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        self.assertIn('_call_ai_api', calls)
        self.assertNotIn('_call_openai_api', calls)

    def test_chat_lock_includes_account_identity(self):
        engine = (ROOT / 'websocket/app/services/xianyu/ai_reply_engine.py').read_text(encoding='utf-8-sig')
        self.assertIn('_get_chat_lock((cookie_id, chat_id))', engine)

    def test_preset_routes_exist_before_account_wildcard(self):
        route = (ROOT / 'backend-web/app/api/routes/ai.py').read_text(encoding='utf-8-sig')
        self.assertIn('@router.get("/presets"', route)
        self.assertLess(route.index('@router.get("/presets"'), route.index('@router.get("/{cookie_id}"'))
