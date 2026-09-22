import time
from unittest.mock import AsyncMock, patch
from test_runtime import DatabaseCase
from common.models.xy_account import XYAccount
from common.services import account_policy as p

class RecoveryChainTests(DatabaseCase):
    async def service(self):
        from common.services.account_recovery import AccountRecovery
        return AccountRecovery('fixture', 7, self.factory)

    async def test_two_nested_attempts_share_persistent_budget(self):
        svc = await self.service()
        job = await svc.start('invalid_credentials')
        await svc.take(job['id'], 'renew')
        with self.assertRaises(p.StaleAccountOperation):
            await svc.take(job['id'], 'password_login')  # 60s cooldown
        async with self.factory() as db:
            a = await db.get(XYAccount, 1); state=p.snapshot(a); state['next_retry_at']=0; p.store(a,state); await db.commit()
        other = await self.service()
        self.assertEqual((await other.start('invalid_credentials'))['id'], job['id'])
        await other.take(job['id'], 'password_login')
        with self.assertRaises(p.StaleAccountOperation): await svc.take(job['id'], 'renew')
        async with self.factory() as db:
            state=p.snapshot(await db.get(XYAccount,1)); self.assertEqual(state['recovery_attempts'],2)

    async def test_recovery_replaces_obsolete_manual_and_automatic_jobs(self):
        from app.services.account_service import AccountService
        for version in ('credential_version', 'config_version', 'generation'):
            for automatic in (False, True):
                with self.subTest(version=version, automatic=automatic):
                    async with self.factory() as db:
                        account = await db.get(XYAccount, 1)
                        account.metadata_json = {}
                        old = p.start_job(account, owner_id=7, kind='password_login')
                        state = p.snapshot(account)
                        state['jobs'][old['id']].update(automatic=automatic, steps=[])
                        state[version] += 1
                        state.update(business_state='ready', reason=None)
                        p.store(account, state)
                        await db.commit()
                    job = await (await self.service()).start('renewal')
                    self.assertIsNotNone(job)
                    self.assertNotEqual(job['id'], old['id'])
                    self.assertEqual(job[version], 1)
                    async with self.factory() as db:
                        account = await db.get(XYAccount, 1)
                        previous = await AccountService(db).get_credential_job(account, old['id'], 7)
                        self.assertEqual(previous['status'], 'superseded')

    async def test_network_proxy_rate_limit_never_start_password_job(self):
        svc=await self.service()
        for reason in ('network','proxy','rate_limit'):
            self.assertIsNone(await svc.start(reason))
        async with self.factory() as db:
            self.assertEqual(p.snapshot(await db.get(XYAccount,1))['jobs'],{})

    async def test_expiry_version_and_cancel_block_attempt_and_late_write(self):
        svc=await self.service(); job=await svc.start('invalid_credentials')
        async with self.factory() as db:
            a=await db.get(XYAccount,1); p.bump_config(a); await db.commit()
        with self.assertRaises(p.StaleAccountOperation): await svc.take(job['id'],'renew')
        self.assertFalse(await svc.complete(job['id'],'unb=101; token=new'))

    async def test_success_preserves_profile_and_does_not_mark_ready(self):
        svc=await self.service(); job=await svc.start('invalid_credentials')
        await svc.take(job['id'],'renew')
        self.assertTrue(await svc.complete(job['id'],'unb=101; token=new'))
        async with self.factory() as db:
            a=await db.get(XYAccount,1); self.assertEqual(a.login_password,'secret')
            self.assertEqual(p.snapshot(a)['business_state'],'unchecked')

    async def test_cookie_write_requires_original_owner_and_versions(self):
        from common.services.account_cookie_service import write_account_credentials
        with patch('common.services.account_cookie_service.async_session_maker',self.factory):
            self.assertTrue(await write_account_credentials('fixture',7,'unb=101; token=new',expected=(0,0,0)))
            self.assertFalse(await write_account_credentials('fixture',7,'unb=101; token=late',expected=(0,0,0)))
            self.assertFalse(await write_account_credentials('fixture',8,'unb=101; token=evil',expected=(1,0,0)))
            with self.assertRaises(ValueError):
                await write_account_credentials('fixture',7,'unb=102; token=evil',expected=(1,0,0))

    async def test_real_recovery_runner_no_blind_password_after_network(self):
        svc=await self.service(); job=await svc.start('invalid_credentials')
        renew=AsyncMock(side_effect=TimeoutError())
        login=AsyncMock()
        self.assertFalse(await svc.run(job['id'],renew=renew,login=login))
        login.assert_not_awaited()
        async with self.factory() as db:
            self.assertEqual(p.snapshot(await db.get(XYAccount,1))['recovery_attempts'],1)

    async def test_real_runner_waits_then_password_no_third_inner_attempt(self):
        svc=await self.service(); job=await svc.start('invalid_credentials')
        from common.services.account_credentials import CredentialRejected
        renew=AsyncMock(side_effect=CredentialRejected('invalid_credentials'))
        login=AsyncMock(return_value='unb=101; token=password')
        async def sleep(_):
            async with self.factory() as db:
                a=await db.get(XYAccount,1);state=p.snapshot(a);state['next_retry_at']=0;p.store(a,state);await db.commit()
        self.assertTrue(await svc.run(job['id'],renew=renew,login=login,sleep=sleep))
        login.assert_awaited_once(); renew.assert_awaited_once()

    async def test_runner_cancel_during_wait_skips_password(self):
        svc=await self.service(); job=await svc.start('invalid_credentials')
        from common.services.account_credentials import CredentialRejected
        login=AsyncMock()
        async def cancel(_):
            async with self.factory() as db:
                a=await db.get(XYAccount,1); p.cancel_job(a,job['id'],owner_id=7); await db.commit()
        self.assertFalse(await svc.run(job['id'],renew=AsyncMock(side_effect=CredentialRejected('invalid_credentials')),login=login,sleep=cancel))
        login.assert_not_awaited()

    async def test_network_second_round_uses_same_total_two_then_pauses(self):
        svc=await self.service(); job=await svc.start('renewal')
        login=AsyncMock()
        self.assertFalse(await svc.run(job['id'],renew=AsyncMock(side_effect=TimeoutError()),login=login))
        self.assertIsNone(await svc.start('renewal'))
        async with self.factory() as db:
            a=await db.get(XYAccount,1);state=p.snapshot(a);state['next_retry_at']=0;p.store(a,state);await db.commit()
        job=await svc.start('renewal')
        self.assertIsNotNone(job)
        self.assertFalse(await svc.run(job['id'],renew=AsyncMock(side_effect=TimeoutError()),login=login))
        async with self.factory() as db:
            state=p.snapshot(await db.get(XYAccount,1));self.assertEqual(state['recovery_attempts'],2);self.assertEqual(state['business_state'],'paused')
        login.assert_not_awaited()

    async def test_unfenced_incremental_merge_is_not_allowed(self):
        from common.services.account_cookie_service import merge_account_cookie_fields
        with patch('common.services.account_cookie_service.async_session_maker',self.factory):
            self.assertIsNone(await merge_account_cookie_fields(1,'fixture',{'token':'late'}))
        async with self.factory() as db:
            self.assertEqual((await db.get(XYAccount,1)).cookie,'unb=101; token=old')

    async def test_scheduled_renew_does_not_launch_unbound_legacy_inner_chain(self):
        from common.services.cookie_renew_api_service import cookie_renew_api_service
        with patch.object(cookie_renew_api_service,'_renew_api_first',AsyncMock()) as old:
            result=await cookie_renew_api_service.renew('unb=101','fixture',source='scheduled_task')
        self.assertFalse(result.success)
        old.assert_not_awaited()

    async def test_mtop_missing_execution_context_has_no_network(self):
        from common.services.xianyu_mtop import mtop_call
        with patch('common.services.xianyu_mtop.aiohttp.ClientSession') as transport:
            result=await mtop_call('fixture','unb=101','fixture.read','1.0',{},owner_id=7)
        self.assertFalse(result['success']);transport.assert_not_called()
