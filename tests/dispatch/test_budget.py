import asyncio
import unittest
from common.services.account_request_budget import AccountRequestBudget, BudgetError, RequestBudgetPolicy
from test_flow import RedisBoundary


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_uses_configured_slowest_bound_not_recovery_attempts(self):
        self.assertEqual(RequestBudgetPolicy.from_risk_config({'min_interval_seconds': 3, 'max_interval_seconds': 5,
            'requests_per_minute': 6, 'recovery_attempts': 2}).interval_ms, 10000)
        for value in [None, {}, {'recovery_attempts': 2}, {'min_interval_seconds': 0},
                      {'min_interval_seconds': True}, {'min_interval_seconds': float('inf')},
                      {'min_interval_seconds': 3, 'max_interval_seconds': 2}]:
            with self.assertRaises(BudgetError): RequestBudgetPolicy.from_risk_config(value)

    async def test_instances_share_budget_and_server_retry_after_never_shortens(self):
        redis = RedisBoundary(); one = AccountRequestBudget(redis); two = AccountRequestBudget(redis)
        policy = RequestBudgetPolicy.from_risk_config({'min_interval_seconds': 1})
        await one.take('a', policy)
        with self.assertRaises(BudgetError): await two.take('a', policy)
        await one.defer('a', 60); await two.defer('a', 1)
        redis.now += 2000
        with self.assertRaises(BudgetError) as caught: await two.take('a', policy)
        self.assertEqual(caught.exception.retry_after, 58)
        await two.take('b', policy)
        redis.now += 60000
        await two.take('a', policy)

    async def test_redis_failure_stops_take_and_defer(self):
        class BrokenRedis:
            async def eval(self, *args): raise OSError('fixture')
        budget = AccountRequestBudget(BrokenRedis())
        with self.assertRaises(BudgetError): await budget.take('a', RequestBudgetPolicy(1000))
        with self.assertRaises(BudgetError): await budget.defer('a', 60)


class ContractValidationTests(unittest.TestCase):
    def test_arbitrary_commands_extra_payload_and_external_image_urls_rejected(self):
        from pydantic import ValidationError
        from common.services.account_dispatch import DispatchRequest
        base = dict(owner_id=7, account_id='a', request_id='r', generation=1, config_version=0, credential_version=0)
        for command, payload in [('eval', {'code': 'print(1)'}), ('sync_items', {'url': 'http://fixture'}),
             ('send_image_message', {'cid': 'c', 'to_user_id': 'u', 'image_url': 'http://127.0.0.1/file'}),
             ('send_image_message', {'cid': 'c', 'to_user_id': 'u', 'image_url': 'https://alicdn.com.attacker.test/x'}),
             ('get_messages', {'cid': 'x', 'limit': 101})]:
            with self.assertRaises(ValidationError): DispatchRequest(**base, command=command, payload=payload)

    def test_registration_is_fixed_and_duplicates_not_replaceable(self):
        from app.services.account_dispatcher import AccountDispatcher
        dispatch = AccountDispatcher(None, None, None)
        for command in ('eval', 'send_text_message'):
            with self.assertRaises(ValueError): dispatch.register(command, lambda *args: None)

    def test_retry_after_parses_seconds_and_http_date_without_negative_wait(self):
        from common.services.account_request_budget import parse_retry_after
        self.assertEqual(parse_retry_after('30', now=0), 30)
        self.assertEqual(parse_retry_after('Thu, 01 Jan 1970 00:01:00 GMT', now=0), 60)
        self.assertIsNone(parse_retry_after('invalid', now=0))
        self.assertIsNone(parse_retry_after('-1', now=0))
