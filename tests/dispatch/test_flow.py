"""S1 HTTP -> RPC client -> WS dispatcher -> existing session -> persistent SQL ledger."""
import asyncio
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from common.models.xy_account import XYAccount
from common.services.account_runtime import AccountRuntime
from common.services.account_execution import ACQUIRE, RENEW, RELEASE
from common.services import account_policy


class RedisBoundary:
    def __init__(self):
        self.data = {}; self.generation = 0; self.now = 1_000_000
    async def get(self, key): return self.data.get(key)
    async def eval(self, script, count, *args):
        from common.services.account_request_budget import TAKE, DEFER
        key = args[0]
        if script == ACQUIRE:
            if key in self.data: return 0
            self.generation += 1
            self.data[key] = f'{args[2]}:{self.generation}'
            return self.generation
        if script in (RENEW, RELEASE):
            if self.data.get(key) != args[1]: return 0
            if script == RELEASE: del self.data[key]
            return 1
        if script == TAKE:
            last, until = self.data.get(key, (0, 0))
            wait = max(until, last + int(args[1])) - self.now
            if wait > 0: return wait
            self.data[key] = (self.now, until)
            return 0
        if script == DEFER:
            last, until = self.data.get(key, (0, 0))
            self.data[key] = (last, max(until, self.now + int(args[1])))
            return 1
        raise AssertionError('unexpected Lua script')


class ClockRedisBoundary(RedisBoundary):
    """Use elapsed wall time at the Redis command seam, not test-only time jumps."""
    def __init__(self):
        import time
        super().__init__()
        self.started = time.monotonic()
        self.elapsed = 0

    async def eval(self, *args):
        import time
        elapsed = int((time.monotonic() - self.started) * 1000)
        self.now += elapsed - self.elapsed
        self.elapsed = elapsed
        return await super().eval(*args)


class SocketBoundary:
    def __init__(self, live):
        self.live = live; self.sent = []; self.reply = {'code': 200, 'body': {'messageId': 'm-fixture'}}
        self.before_reply = None
    async def send(self, text):
        packet = json.loads(text); self.sent.append(packet)
        if self.before_reply: await self.before_reply()
        if self.reply is not None:
            mid = packet['headers']['mid']
            response = {**self.reply, 'headers': {'mid': mid}}
            self.live._pending_mid_futures.pop(mid).set_result(response)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from common.models.account_operation import AccountOperation
        from common.models.reply_state import TABLES
        from common.models.xy_personal_blacklist import XYPersonalBlacklist
        from common.models.xy_platform_blacklist import XYPlatformBlacklist
        from common.services.account_request_budget import AccountRequestBudget
        from common.services.account_dispatch import DispatchRequest
        from app.services.account_dispatcher import AccountDispatcher
        self.Request = DispatchRequest
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + self.tmp.name + '/dispatch.db')
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            for table in (XYAccount.__table__, AccountOperation.__table__, XYPersonalBlacklist.__table__, XYPlatformBlacklist.__table__, *TABLES):
                await conn.run_sync(table.create)
        async with self.sessions() as db:
            db.add(XYAccount(id=1, owner_id=7, account_id='fixture', cookie='unb=101; _m_h5_tk=offline_fixture;',
                             unb='101', login_method='manual', status='active',
                             metadata_json={'xy_runtime': {'config_values': {'risk': {'min_interval_seconds': 1}}}}))
            await db.commit()
        self.redis = ClockRedisBoundary()
        self.runtime = AccountRuntime('fixture', 7, self.redis, self.sessions)
        await self.runtime.start()
        await self.runtime.record_connection(True, verified=True)
        self.live = SimpleNamespace(_account_runtime=self.runtime,
                                    _check_account_execution=self.runtime.check,
                                    _pending_mid_futures={}, myid='101')
        self.socket = SocketBoundary(self.live); self.live.ws = self.socket
        self.manager = SimpleNamespace(instances={'fixture': self.live})
        self.budget = AccountRequestBudget(self.redis)
        self.dispatcher = AccountDispatcher(self.manager, self.sessions, self.budget, response_timeout=.025)

    async def asyncTearDown(self):
        if hasattr(self, 'runtime'): await self.runtime.close()
        if hasattr(self, 'engine'): await self.engine.dispose()
        if hasattr(self, 'tmp'): self.tmp.cleanup()

    def request(self, command='send_text_message', request_id='r1', **payload):
        if command == 'send_text_message': payload = {'cid': 'chat', 'to_user_id': 'buyer', 'text': 'hello', **payload}
        return self.Request(request_id=request_id, owner_id=7, account_id='fixture',
                            generation=self.runtime.generation, credential_version=0, config_version=0,
                            command=command, payload=payload)

    async def test_disconnected_account_rejects_business_before_platform_send(self):
        from common.services.account_dispatch import DispatchError
        await self.runtime.record_connection(False)
        with self.assertRaises(DispatchError) as caught:
            await self.dispatcher.execute(self.request())
        self.assertEqual(caught.exception.code, 'account_not_ready')
        self.assertEqual(self.socket.sent, [])

    async def test_send_ack_and_durable_dedup_after_dispatcher_restart(self):
        from app.services.account_dispatcher import AccountDispatcher
        first = await self.dispatcher.execute(self.request())
        self.assertEqual(first.status, 'confirmed')
        self.assertEqual(first.result['messageId'], 'm-fixture')
        restarted = AccountDispatcher(self.manager, self.sessions, self.budget)
        repeated = await restarted.execute(self.request())
        self.assertEqual(first.model_dump(), repeated.model_dump())
        self.assertEqual(len(self.socket.sent), 1)

    async def test_four_chat_commands_use_same_socket_and_no_login(self):
        cases = [('get_conversations', {'limit': 5}, '/r/Conversation/listNewestPagination'),
                 ('get_messages', {'cid': 'chat', 'limit': 7}, '/r/MessageManager/listUserMessages'),
                 ('send_text_message', {}, '/r/MessageSend/sendByReceiverScope'),
                 ('send_image_message', {'cid': 'chat', 'to_user_id': 'buyer',
                  'image_url': 'https://img.alicdn.com/imgextra/fixture.jpg', 'width': 30, 'height': 40},
                  '/r/MessageSend/sendByReceiverScope')]
        for index, (command, payload, lwp) in enumerate(cases):
            self.redis.now += 2000
            result = await self.dispatcher.execute(self.request(command, 'r'+str(index), **payload))
            self.assertEqual(result.status, 'confirmed')
            self.assertEqual(self.socket.sent[-1]['lwp'], lwp)
        self.assertEqual(self.redis.generation, 1)
        self.assertEqual(len(self.socket.sent), 4)

    async def test_unknown_timeout_is_persisted_and_never_resent(self):
        self.socket.reply = None
        result = await self.dispatcher.execute(self.request())
        self.assertEqual(result.status, 'unknown')
        self.assertEqual((await self.dispatcher.execute(self.request())).status, 'unknown')
        self.assertEqual(len(self.socket.sent), 1)
        self.assertEqual(self.live._pending_mid_futures, {})

    async def test_missing_positive_ack_is_unknown_not_success(self):
        self.socket.reply = {'body': {}}
        self.assertEqual((await self.dispatcher.execute(self.request())).status, 'unknown')

    async def test_platform_rejection_is_explicit_failure(self):
        self.socket.reply = {'code': 200, 'body': {'reason': 'CSI_FORBID'}}
        self.assertEqual((await self.dispatcher.execute(self.request())).status, 'failed')

    async def test_owner_and_request_fingerprint_fences(self):
        from common.services.account_dispatch import DispatchError
        bad = self.request().model_copy(update={'owner_id': 8})
        with self.assertRaises(DispatchError) as caught: await self.dispatcher.execute(bad)
        self.assertEqual(caught.exception.code, 'account_not_found')
        await self.dispatcher.execute(self.request())
        with self.assertRaises(DispatchError) as caught:
            await self.dispatcher.execute(self.request(text='changed'))
        self.assertEqual(caught.exception.code, 'request_conflict')
        self.assertEqual(len(self.socket.sent), 1)

    async def test_stale_generation_and_new_config_rejected_before_send(self):
        from common.services.account_dispatch import DispatchError
        with self.assertRaises(DispatchError):
            await self.dispatcher.execute(self.request().model_copy(update={'generation': 0}))
        async with self.sessions() as db:
            a = await db.get(XYAccount, 1); account_policy.bump_config(a); await db.commit()
        with self.assertRaises(DispatchError): await self.dispatcher.execute(self.request())
        self.assertEqual(self.socket.sent, [])

    async def test_change_after_budget_check_stops_external_send(self):
        original = self.budget.take
        async def take(*args, **kwargs):
            await original(*args, **kwargs)
            async with self.sessions() as db:
                a = await db.get(XYAccount, 1); a.status = 'disabled'; await db.commit()
        self.budget.take = take
        result = await self.dispatcher.execute(self.request())
        self.assertEqual(result.status, 'failed')
        self.assertEqual(self.socket.sent, [])

    async def test_change_while_inflight_makes_result_unknown(self):
        async def changed():
            async with self.sessions() as db:
                a = await db.get(XYAccount, 1); account_policy.bump_config(a); await db.commit()
        self.socket.before_reply = changed
        self.assertEqual((await self.dispatcher.execute(self.request())).status, 'unknown')

    async def test_budget_is_shared_by_commands_and_waits_before_next_send(self):
        import time
        await self.dispatcher.execute(self.request())
        started = time.monotonic()
        result = await self.dispatcher.execute(self.request('get_conversations', 'r2'))
        self.assertEqual(result.status, 'confirmed', result)
        self.assertGreaterEqual(time.monotonic() - started, .8)
        self.assertEqual(len(self.socket.sent), 2)

    async def test_waiting_budget_stops_when_account_is_disabled_before_send(self):
        await self.budget.defer('fixture', .3)
        task = asyncio.create_task(self.dispatcher.execute(self.request()))
        await asyncio.sleep(.04)
        async with self.sessions() as session:
            account = await session.get(XYAccount, 1)
            account.status = 'disabled'
            await session.commit()
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.error_code, 'execution_lost')
        self.assertEqual(self.socket.sent, [])

    async def test_unregistered_business_reports_unavailable_without_send(self):
        from common.services.account_dispatch import DispatchError
        with self.assertRaises(DispatchError) as caught:
            await self.dispatcher.execute(self.request('sync_items', 'r3'))
        self.assertEqual(caught.exception.code, 'command_unavailable')
        self.assertEqual(self.socket.sent, [])

    async def test_client_route_auth_and_status_chain(self):
        import httpx
        from fastapi import FastAPI
        from app.api.routes.account_operations import router, get_account_dispatcher
        from app.core.config import get_settings
        path = Path(__file__).parents[2] / 'backend-web/app/services/account_dispatch_client.py'
        spec = importlib.util.spec_from_file_location('dispatch_client_fixture', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[get_account_dispatcher] = lambda: self.dispatcher
        settings = get_settings(); old = settings.internal_api_token
        settings.internal_api_token = 'fixture-internal-token-' + 'x'*32
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as http:
                client = module.AccountDispatchClient('http://fixture', settings.internal_api_token, http=http)
                denied = await http.post('/internal/account-operations', json=self.request().model_dump())
                self.assertEqual(denied.status_code, 401)
                accepted = await client.execute(self.request())
                self.assertEqual(accepted.status, 'confirmed')
                queried = await client.get_operation(7, 'fixture', 'r1')
                self.assertEqual(queried.status, 'confirmed')
                missing = await http.get('/internal/account-operations/r1', params={'owner_id': 8, 'account_id': 'fixture'},
                                         headers={'X-Internal-Token': settings.internal_api_token})
                self.assertEqual(missing.status_code, 404)
        finally: settings.internal_api_token = old

    async def test_repeat_id_after_generation_change_returns_original_operation(self):
        first = await self.dispatcher.execute(self.request())
        replay = self.request().model_copy(update={'generation': 9, 'config_version': 9})
        result = await self.dispatcher.execute(replay)
        self.assertEqual(result.model_dump(), first.model_dump())
        self.assertEqual(len(self.socket.sent), 1)

    async def test_expired_submitted_claim_is_unknown_without_resend(self):
        await self.dispatcher.store.claim(self.request(), 0)
        result = await self.dispatcher.execute(self.request())
        self.assertEqual(result.status, 'unknown')
        self.assertEqual(self.socket.sent, [])

    async def test_concurrent_duplicate_has_one_external_send(self):
        results = await asyncio.gather(*(self.dispatcher.execute(self.request()) for _ in range(6)))
        self.assertTrue(all(r.status in {'confirmed', 'submitted'} for r in results))
        self.assertEqual(len(self.socket.sent), 1)

    async def test_cancellation_after_send_keeps_unknown(self):
        self.socket.reply = None
        self.dispatcher.response_timeout = 10
        task = asyncio.create_task(self.dispatcher.execute(self.request()))
        for _ in range(200):
            if self.socket.sent: break
            await asyncio.sleep(.001)
        self.assertTrue(self.socket.sent)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual((await self.dispatcher.execute(self.request())).status, 'unknown')

    async def test_new_filter_added_during_budget_io_is_applied_before_send(self):
        from common.models.reply_state import advanced_filters
        from sqlalchemy import insert
        original = self.budget.take
        async def take(*args, **kwargs):
            await original(*args, **kwargs)
            async with self.sessions() as db:
                await db.execute(insert(advanced_filters).values(id='f', account_id='fixture', pattern='hello',
                    match_mode='contains', source='all', actions=['skip_reply'], enabled=True))
                await db.commit()
        self.budget.take = take
        result = await self.dispatcher.execute(self.request())
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.error_code, 'outbound_filtered')
        self.assertEqual(self.socket.sent, [])


    async def test_polish_rate_and_flower_use_existing_fenced_http_session(self):
        from common.models.xy_order import XYOrder
        from common.models.xy_catalog_item import XYCatalogItem
        async with self.engine.begin() as conn:
            await conn.run_sync(XYOrder.__table__.create)
            await conn.run_sync(XYCatalogItem.__table__.create)
        async with self.sessions() as db:
            db.add(XYOrder(id=1, owner_id=7, account_id='fixture', order_no='o1', status='completed'))
            db.add(XYCatalogItem(id=1, owner_id=7, account_pk=1, item_id='i1', created_at=datetime.now()))
            await db.commit()
        class Response:
            status = 200; headers = {}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self): return {'ret': ['SUCCESS::OK'], 'data': {}}
        calls = []
        class HTTP:
            def post(self, url, **kwargs):
                calls.append((url, kwargs)); return Response()
        self.live.session = HTTP()
        cases = [('polish_item', {'item_id': 'i1'}, 'mtop.taobao.idle.item.polish'),
                 ('rate_buyer', {'order_no': 'o1'}, 'mtop.taobao.idle.rate.create'),
                 ('request_red_flower', {'order_no': 'o1'}, 'mtop.taobao.idlemessage.red.flower')]
        for index, (command, payload, api) in enumerate(cases):
            self.redis.now += 2000
            result = await self.dispatcher.execute(self.request(command, 'http'+str(index), **payload))
            self.assertEqual(result.status, 'confirmed')
            self.assertTrue(result.result['success'])
            self.assertEqual(calls[-1][1]['params']['api'], api)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.redis.generation, 1)
        self.assertEqual(self.socket.sent, [])

    async def test_business_command_rejects_foreign_item_without_http(self):
        from common.models.xy_catalog_item import XYCatalogItem
        async with self.engine.begin() as conn: await conn.run_sync(XYCatalogItem.__table__.create)
        async with self.sessions() as db:
            db.add(XYCatalogItem(id=1, owner_id=99, account_pk=2, item_id='foreign', created_at=datetime.now()))
            await db.commit()
        result = await self.dispatcher.execute(self.request('polish_item', item_id='foreign'))
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.error_code, 'item_not_found')

    async def test_retry_after_is_returned_and_shared_with_next_command(self):
        self.socket.reply = {'code': 429, 'body': {'retryAfter': 60}}
        result = await self.dispatcher.execute(self.request())
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.retry_after, 60)
        self.redis.now += 2000
        blocked = await self.dispatcher.execute(self.request('get_messages', 'next', cid='chat'))
        self.assertEqual(blocked.error_code, 'budget_wait')
        self.assertGreater(blocked.retry_after, 57)
        self.assertLessEqual(blocked.retry_after, 58)
        self.assertEqual(len(self.socket.sent), 1)

    async def test_missing_risk_config_never_creates_business_default(self):
        async with self.sessions() as db:
            account = await db.get(XYAccount, 1)
            state = account_policy.snapshot(account); state['config_values'] = {}
            account_policy.store(account, state); await db.commit()
        result = await self.dispatcher.execute(self.request())
        self.assertEqual(result.error_code, 'risk_not_configured')
        self.assertEqual(self.socket.sent, [])

    async def test_reply_blacklist_and_program_pause_checked_in_executor(self):
        from common.models.xy_personal_blacklist import XYPersonalBlacklist
        async with self.sessions() as db:
            db.add(XYPersonalBlacklist(id=1, owner_id=7, account_id='fixture', buyer_id='buyer', is_enabled=True))
            await db.commit()
        denied = await self.dispatcher.execute(self.request())
        self.assertEqual(denied.error_code, 'outbound_blacklisted')
        from common.services.reply_state import ReplyState
        await ReplyState(self.sessions).pause('fixture', 'chat', 10)
        paused = await self.dispatcher.execute(self.request(request_id='p', to_user_id='someone', origin='program'))
        self.assertEqual(paused.error_code, 'conversation_paused')
        self.assertEqual(self.socket.sent, [])

    async def test_client_timeout_is_unknown_and_never_retries_post(self):
        import httpx
        from common.services.account_dispatch import AccountDispatchClient
        calls = []
        async def transport(req):
            calls.append(req)
            raise httpx.ReadTimeout('fixture')
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            client = AccountDispatchClient('http://fixture', 'x'*40, http=http)
            result = await client.execute(self.request())
            self.assertEqual(result.status, 'unknown')
            self.assertEqual(result.request_id, 'r1')
            self.assertEqual(len(calls), 1)

    async def test_timeout_keeps_correlation_ids_persisted_before_external_send(self):
        self.socket.reply = None
        result = await self.dispatcher.execute(self.request())
        self.assertEqual(result.status, 'unknown')
        self.assertIsNotNone(result.result)
        self.assertEqual(result.result['mid'], self.socket.sent[0]['headers']['mid'])
        self.assertEqual(result.result['uuid'], self.socket.sent[0]['body'][0]['uuid'])
        self.assertEqual(result.result['cid'], 'chat')
        self.assertNotIn('text', result.result)

    async def test_public_multirequest_hook_rechecks_each_page(self):
        from common.services.account_dispatch import before_platform_request
        calls = []
        async def sync(context, payload):
            await before_platform_request(context.request.account_id)
            calls.append('first-page')
            async with self.sessions() as db:
                account = await db.get(XYAccount, 1)
                account_policy.bump_config(account); await db.commit()
            self.redis.now += 2000
            await before_platform_request(context.request.account_id)
            calls.append('second-page')
            return {'success': True}
        self.dispatcher.register('sync_items', sync)
        result = await self.dispatcher.execute(self.request('sync_items'))
        self.assertEqual(result.status, 'unknown')
        self.assertEqual(calls, ['first-page'])
        with self.assertRaises(Exception): await before_platform_request('fixture')

    async def test_existing_chat_128_character_request_id_is_preserved(self):
        request_id = 'r' * 128
        result = await self.dispatcher.execute(self.request(request_id=request_id))
        self.assertEqual(result.request_id, request_id)
        self.assertEqual(result.status, 'confirmed')

    async def test_chat_protocol_preserves_existing_mid_and_uuid_wire_format(self):
        await self.dispatcher.execute(self.request())
        packet = self.socket.sent[0]
        self.assertRegex(packet['headers']['mid'], r'^\d+ 0$')
        self.assertRegex(packet['body'][0]['uuid'], r'^-\d+1$')
