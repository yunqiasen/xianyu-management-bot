"""S1 -> real recovery policy -> S2 credential verifier, no platform login."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from fastapi import FastAPI
import test_flow as fixtures
import unittest
from common.models.xy_account import XYAccount
from common.services.account_renewal import AccountRenewalClient

class RenewalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FlowTests.asyncSetUp
    asyncTearDown = fixtures.FlowTests.asyncTearDown

async def test_renewal_rpc_updates_versions_without_enabling_disabled_account(self):
    from app.api.routes.account_operations import router,get_account_dispatcher
    from app.api.deps import require_internal_auth
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_account_dispatcher]=lambda:self.dispatcher
    app.dependency_overrides[require_internal_auth]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://executor') as http:
        client=AccountRenewalClient('http://executor','f'*32,http=http)
        async with self.sessions() as db:
            account=await db.get(XYAccount,1)
            with patch('common.services.account_credentials.validate_credentials',AsyncMock(return_value='unb=101; token=renewed')):
                result=await client.renew(account)
            self.assertEqual(result['status'],'verified')
            self.assertEqual(self.live.cookies_str,'unb=101; token=renewed')
            self.assertEqual(self.runtime.version,(1,0))
            # Same old snapshot cannot start another network request.
            with patch('common.services.account_credentials.validate_credentials',AsyncMock()) as network:
                stale=await client.renew(account)
            network.assert_not_awaited();self.assertEqual(stale['status'],'failed')
        async with self.sessions() as db:
            account=await db.get(XYAccount,1);account.status='inactive';await db.commit()
            with patch('common.services.account_credentials.validate_credentials',AsyncMock()) as network:
                disabled=await client.renew(account)
            network.assert_not_awaited();self.assertEqual(disabled['status'],'skipped')

RenewalTests.test_renewal_rpc_updates_versions_without_enabling_disabled_account=test_renewal_rpc_updates_versions_without_enabling_disabled_account

async def test_lost_lease_during_renewal_does_not_persist_credentials(self):
    from common.services.account_renewal import renew_on_executor,RenewalRequest
    from common.services.account_policy import snapshot
    async with self.sessions() as db:
        account=await db.get(XYAccount,1);state=snapshot(account)
        request=RenewalRequest(owner_id=7,account_id='fixture',**{k:state[k] for k in ('generation','credential_version','config_version')})
    async def lose_lease(*args):
        self.redis.data.pop(self.runtime.lease.key,None)
        return 'unb=101; token=late'
    with patch('common.services.account_credentials.validate_credentials',AsyncMock(side_effect=lose_lease)):
        try: await renew_on_executor(self.dispatcher,request)
        except Exception: pass
    async with self.sessions() as db:
        account=await db.get(XYAccount,1)
        self.assertEqual(account.cookie,'unb=101; _m_h5_tk=offline_fixture;')

RenewalTests.test_lost_lease_during_renewal_does_not_persist_credentials=test_lost_lease_during_renewal_does_not_persist_credentials
