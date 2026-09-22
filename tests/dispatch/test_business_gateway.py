import unittest
from types import SimpleNamespace
import httpx
from common.services.account_dispatch import AccountDispatchClient
import test_flow as fixtures


class BusinessGatewayTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.FlowTests.asyncSetUp
    asyncTearDown=fixtures.FlowTests.asyncTearDown
    request=fixtures.FlowTests.request

    async def test_transport_maps_definite_and_unknown_results_without_retry(self):
        from common.services.account_business_client import dispatch_business
        seen=[]
        async def worker(request):
            import json
            body=json.loads(request.content);seen.append(body)
            return httpx.Response(200,json={**{key:body[key] for key in ('request_id','owner_id','account_id',
                'command','generation','credential_version','config_version')},'status':'unknown',
                'error_code':'result_unverified'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(worker)) as http:
            rpc=AccountDispatchClient('http://fixture','fixture-'*8,http=http)
            result=await dispatch_business(SimpleNamespace(account_id='fixture',owner_id=7),'polish_item',
                {'item_id':'item'},rpc=rpc,sessions=self.sessions,request_id='polish-1')
        self.assertFalse(result['success']);self.assertTrue(result['unknown'])
        self.assertFalse(result['definitive_failure']);self.assertEqual(len(seen),1)
        self.assertNotIn('cookie',seen[0]);self.assertNotIn('proxy',seen[0])
