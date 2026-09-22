from support import *
from app.services.auth import AuthService
class AuthTests(DatabaseCase):
    async def test_username_five_failures_not_three(self):
        service = AuthService(self.session)
        for _ in range(3):
            await service.authenticate_by_username('seller', 'wrong')
        user, _ = await service.authenticate_by_username('seller', 'correct')
        self.assertIsNotNone(user, '规格为15分钟5次，旧3次锁定应移除')
    async def test_unknown_and_known_user_errors_match(self):
        service = AuthService(self.session)
        _, known = await service.authenticate_by_username('seller', 'wrong')
        _, unknown = await service.authenticate_by_username('missing', 'wrong')
        self.assertEqual(known, unknown)
    async def test_ip_threshold_sliding_window_and_targeted_unlock(self):
        from app.services.login_protection_service import LoginProtectionService
        now=[1000.0]; guard=LoginProtectionService(self.session,clock=lambda:now[0])
        for n in range(20): await guard.record(f'unknown-{n}','192.0.2.1')
        self.assertTrue(await guard.check('seller','192.0.2.1'))
        await guard.unlock('username','seller',1)
        self.assertTrue(await guard.check('seller','192.0.2.1'))
        await guard.unlock('ip','192.0.2.1',1)
        self.assertFalse(await guard.check('seller','192.0.2.1'))
        for _ in range(5): await guard.record('seller','192.0.2.2')
        self.assertTrue(await guard.check('seller','192.0.2.3'))
        now[0]+=901
        self.assertFalse(await guard.check('seller','192.0.2.3'))
    async def test_email_alias_cannot_reset_username_lock(self):
        service=AuthService(self.session)
        for _ in range(5): await service.authenticate_by_username('seller','bad',ip='192.0.2.4')
        user,_=await service.authenticate_by_email('seller@example.com','correct',ip='192.0.2.5')
        self.assertIsNone(user)
    async def test_email_code_login_also_checks_ip(self):
        from app.api.routes import auth
        from app.api import deps
        from app.services.login_protection_service import LoginProtectionService
        from fastapi import FastAPI
        from unittest.mock import AsyncMock,patch
        import httpx
        guard=LoginProtectionService(self.session)
        for n in range(20):await guard.record(f'u{n}','127.0.0.1')
        app=FastAPI();app.include_router(auth.router)
        app.dependency_overrides[deps.get_db_session]=lambda:self.session
        app.dependency_overrides[deps.get_auth_service]=lambda:AuthService(self.session)
        with patch('app.services.system_setting_service.SystemSettingService.list_settings',new=AsyncMock(return_value={'login_captcha_enabled':'false'})),patch.object(auth,'check_email_code',return_value=(True,'ok')):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as c:
                r=await c.post('/login',json={'email':'seller@example.com','verification_code':'123456'})
                self.assertFalse(r.json()['success'])
    async def test_role_change_has_atomic_audit(self):
        from app.services.user_service import UserService
        from common.schemas.user import AdminUserUpdate
        from common.models.admin_control import AdminAudit
        from sqlalchemy import select
        service=UserService(self.session)
        import inspect
        self.assertIn('actor_id',inspect.signature(service.update_admin_user).parameters)
        user=await self.session.get(User,2)
        await service.update_admin_user(user,AdminUserUpdate(role=UserRole.OPERATOR),actor_id=1)
        audit=(await self.session.execute(select(AdminAudit).where(AdminAudit.action=='user_update'))).scalar_one()
        self.assertEqual(audit.actor_id,1);self.assertNotIn('password',str(audit.details))

class LoginProtectionConfigurationTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from fastapi import FastAPI
        from httpx import AsyncClient, ASGITransport
        from app.api import deps
        from app.api.routes import admin, auth
        from common.models.system_setting import SystemSetting
        async with self.engine.begin() as connection:
            await connection.run_sync(SystemSetting.__table__.create)
        user = await self.session.get(User, 1)
        user.role = UserRole.ADMIN
        self.session.add(SystemSetting(key='login_captcha_enabled', value='false'))
        await self.session.commit()
        app = FastAPI()
        app.include_router(admin.router, prefix='/admin')
        app.include_router(auth.router, prefix='/auth')
        app.dependency_overrides[deps.get_db_session] = lambda: self.session
        app.dependency_overrides[deps.get_auth_service] = lambda: AuthService(self.session)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url='http://fixture')
        self.headers = {'Authorization': 'Bearer ' + AuthService(self.session).create_access_token(user)}

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_admin_config_has_validated_versions_and_changes_real_login_threshold(self):
        response = await self.client.get('/admin/login-protection/config', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        initial = response.json()['data']
        self.assertEqual(initial, {'window_seconds': 900, 'lock_seconds': 900,
            'username_threshold': 5, 'ip_threshold': 20, 'version': 0})
        payload = {**initial, 'username_threshold': 2, 'expected_version': 0}
        del payload['version']
        saved = await self.client.put('/admin/login-protection/config', json=payload, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['data']['version'], 1)
        self.assertEqual((await self.client.put('/admin/login-protection/config', json=payload, headers=self.headers)).status_code, 409)
        self.assertEqual((await self.client.put('/admin/login-protection/config', json={**payload, 'expected_version': 1,
            'window_seconds': 0}, headers=self.headers)).status_code, 422)
        self.assertEqual((await self.client.put('/admin/login-protection/config', json={**payload, 'expected_version': 1,
            'username_threshold': True}, headers=self.headers)).status_code, 422)
        for _ in range(2):
            failed = await self.client.post('/auth/login', json={'username': 'seller', 'password': 'wrong'})
            self.assertFalse(failed.json()['success'])
        locked = await self.client.post('/auth/login', json={'username': 'seller', 'password': 'correct'})
        self.assertFalse(locked.json()['success'], locked.text)
        # Raising thresholds does not silently remove an existing lock.
        changed = await self.client.put('/admin/login-protection/config', json={**payload,
            'expected_version': 1, 'username_threshold': 5}, headers=self.headers)
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertFalse((await self.client.post('/auth/login', json={'username': 'seller', 'password': 'correct'})).json()['success'])
        await self.client.post('/admin/login-protection/unlock', json={'dimension': 'username', 'subject': 'seller'}, headers=self.headers)
        self.assertTrue((await self.client.post('/auth/login', json={'username': 'seller', 'password': 'correct'})).json()['success'])
        from common.models.admin_control import AdminAudit
        from sqlalchemy import select
        audits = (await self.session.scalars(select(AdminAudit).where(AdminAudit.action == 'login_protection_config'))).all()
        self.assertEqual([a.details['version'] for a in audits], [1, 2])
        self.assertNotIn('password', str([a.details for a in audits]))

    async def test_non_admin_has_no_protection_configuration_access(self):
        token = AuthService(self.session).create_access_token(await self.session.get(User, 2))
        response = await self.client.get('/admin/login-protection/config', headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 403, response.text)
