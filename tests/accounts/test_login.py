from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[2]/'backend-web'))
import unittest
from unittest.mock import AsyncMock, patch
from app.services.password_login import flow

class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_login_never_saves_late_success(self):
        with patch.object(flow,'_save_and_start',AsyncMock(return_value=(False,'ok'))) as save:
            await flow._finish_success({'cancelled':True},account_id='fixture',account='seller',
                    password='secret',show_browser=False,owner_id=7,cookies_str='unb=101',unb='101')
        save.assert_not_awaited()

    async def test_slider_budget_is_shared_two_attempts(self):
        self.assertEqual(flow._MAX_SLIDER_ROUNDS,2)

    async def test_cancel_releases_task_and_keeps_terminal_status(self):
        import asyncio
        from app.services.password_login.manager import PasswordLoginManager
        manager=PasswordLoginManager()
        task=asyncio.create_task(asyncio.sleep(60))
        manager.sessions['pl_fixture']={'owner_id':7,'status':'verification_required','timestamp':__import__('time').time(),
                                       '_task':task,'face_qr_url':'fixture-image'}
        self.assertTrue(manager.cancel('pl_fixture',7))
        requested = bool(task.cancelling())
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertTrue(requested)
        self.assertEqual(manager.get_status('pl_fixture',7)['status'],'cancelled')
        self.assertIsNone(manager.get_status('pl_fixture',7)['face_qr_url'])
