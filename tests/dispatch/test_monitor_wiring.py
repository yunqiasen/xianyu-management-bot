import unittest
import test_flow as fixtures


class MonitorCommandTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FlowTests.asyncSetUp
    asyncTearDown = fixtures.FlowTests.asyncTearDown
    request = fixtures.FlowTests.request

    async def test_candidate_disabled_reaches_guard_without_external_request(self):
        request = self.Request(owner_id=7, account_id='fixture', generation=self.runtime.generation,
            credential_version=0, config_version=0, command='monitor_search_page', request_id='monitor-1',
            payload=dict(task_id=1, account_id='fixture', keyword='fixture', page=1, generation=1,
                query_key='a'*64, request_id='monitor-1', monitor_type='listing'))
        result = await self.dispatcher.execute(request)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.error_code, 'monitor_v2_disabled')
        self.assertEqual(self.socket.sent, [])
