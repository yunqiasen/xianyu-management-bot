"""S1 服务入口；数据库会话/平台调用用边界替身，执行仓库真实服务方法。"""
import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from test_policy import p, account


def load_service():
    path = Path(__file__).parents[2] / 'backend-web/app/services/account_service.py'
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'AccountService')
    ns = dict(datetime=datetime, UTC=timezone.utc, XYAccount=SimpleNamespace,
              clear_cookie_refresh_snapshot=lambda value: value, account_policy=p,
              select=lambda *a: Query(), update=lambda *a: Query())
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])), str(path), 'exec'), ns)
    return ns['AccountService']


class Query:
    def where(self, *args): return self
    def with_for_update(self): return self
    def execution_options(self, **args): return self


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.a = account()
        self.db = SimpleNamespace(add=Mock(), commit=AsyncMock(), refresh=AsyncMock(),
                                  delete=AsyncMock(), execute=AsyncMock())
        self.svc = load_service()(self.db)
        # 行锁返回同一夹具，外部存储是测试接缝。
        self.svc._lock_account = AsyncMock(return_value=self.a)

    async def test_edit_login_preserves_blank_password(self):
        await self.svc.update_login_info(self.a, username='', login_password='******', show_browser=False)
        self.assertEqual(self.a.username, 'seller')
        self.assertEqual(self.a.login_password, 'secret')
        self.db.commit.assert_awaited_once()

    async def test_qr_refresh_does_not_enable_disabled_account(self):
        self.svc.get_account_by_unb = AsyncMock(return_value=self.a)
        self.svc._check_identity_owner = AsyncMock()
        result, created = await self.svc.upsert_account_from_qr(7, 'unb=101; token=new', '101')
        self.assertFalse(created)
        self.assertEqual(result.status, 'disabled')
        self.assertEqual(result.disable_reason, 'manual')
        self.assertEqual(result.login_password, 'secret')

    async def test_password_refresh_preserves_manual_disable_and_profile(self):
        self.svc.get_account_by_identifier = AsyncMock(return_value=self.a)
        self.svc._check_identity_owner = AsyncMock()
        result, created = await self.svc.upsert_account_from_password(7, 'a', '', '******', 'unb=101; token=new', '101')
        self.assertFalse(created)
        self.assertEqual(result.status, 'disabled')
        self.assertEqual(result.login_password, 'secret')

    async def test_explicit_version_late_cookie_update_fails(self):
        await self.svc.update_cookie(self.a, 'unb=101; token=new', expected_version=0)
        with self.assertRaises(p.StaleAccountOperation):
            await self.svc.update_cookie(self.a, 'unb=101; token=old', expected_version=0)
        self.assertEqual(self.a.cookie, 'unb=101; token=new')

if __name__ == '__main__': unittest.main()
