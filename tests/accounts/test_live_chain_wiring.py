"""加载真实 WS 管理器；只替换平台边界，避免启动服务和真实交易。"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from test_runtime import DatabaseCase
from common.models.xy_account import XYAccount
from common.services import account_policy as p

class LiveWiringTests(DatabaseCase):
    async def test_ws_password_path_uses_persistent_budget_not_old_nested_renew(self):
        path=Path(__file__).parents[2]/'websocket/app/services/xianyu/cookie_token_manager.py'
        spec=importlib.util.spec_from_file_location('accounts_ws_cookie_token',path)
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        parent=SimpleNamespace(cookie_id='fixture',user_id=7,cookies_str='unb=101; token=old',cookies={'unb':'101'},_account_runtime=None,_safe_str=str)
        manager=mod.CookieTokenManager(parent)
        manager.send_token_refresh_notification=AsyncMock()
        with (patch('common.services.cookie_renew_api_service.cookie_renew_api_service.renew',AsyncMock(side_effect=TimeoutError())),
              patch('common.services.account_recovery.AccountRecovery.__init__', lambda obj,a,o: (setattr(obj,'account_id',a),setattr(obj,'owner_id',o),setattr(obj,'sessions',self.factory)) and None),
              patch('common.services.account_recovery.AccountRecovery.run',AsyncMock(return_value=False)) as run,
              patch.object(manager,'_load_account_record',AsyncMock(return_value=XYAccount(owner_id=7,account_id='fixture',status='active')))):
            await manager.try_password_login_refresh('Session过期')
        run.assert_awaited_once()
        async with self.factory() as db:
            self.assertEqual(len(p.snapshot(await db.get(XYAccount,1))['jobs']),1)

    async def test_all_token_refreshes_enter_same_persistent_runner(self):
        path=Path(__file__).parents[2]/'websocket/app/services/xianyu/cookie_token_manager.py'
        spec=importlib.util.spec_from_file_location('accounts_token_refresh',path)
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        parent=SimpleNamespace(cookie_id='fixture',user_id=7,cookies_str='unb=101',cookies={'unb':'101'},
            _account_runtime=None,_safe_str=str,current_token=None,last_token_refresh_status='',last_message_received_time=0)
        manager=mod.CookieTokenManager(parent)
        manager.send_token_refresh_notification=AsyncMock()
        manager._get_cached_token=AsyncMock(return_value=None)
        manager._is_local_slider_disabled=AsyncMock(return_value=False)
        manager._get_processing_risk_control_skip_result=AsyncMock(return_value=(True,None))
        with (patch('common.services.account_recovery.AccountRecovery.__init__',lambda obj,a,o: (setattr(obj,'account_id',a),setattr(obj,'owner_id',o),setattr(obj,'sessions',self.factory)) and None),
              patch('common.services.account_recovery.AccountRecovery.run',AsyncMock(return_value=False)) as run):
            await manager.refresh_token()
        run.assert_awaited_once()
