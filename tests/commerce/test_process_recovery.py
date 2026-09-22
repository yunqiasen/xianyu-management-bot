"""S1/S2: SIGKILL real workers around each external side effect, then restart."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from uuid import uuid4

from aiohttp import web
from fastapi import FastAPI
import httpx
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.api import deps
from app.api.routes.order_commerce import router
from common.models.card import Card
from common.models.delivery_intent import DeliveryIntent
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.services.card_purchase import query_card
from common.services.delivery_execution import DeliveryExecution

ROOT = Path(__file__).resolve().parents[2]


class ProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = os.environ.get('XYMB_COMMERCE_MYSQL_URL')
        if not url:
            raise RuntimeError('XYMB_COMMERCE_MYSQL_URL required')
        parsed = make_url(url)
        if parsed.host not in {'127.0.0.1', 'localhost'} or not (parsed.database or '').startswith('xy_commerce_fixture_'):
            raise ValueError('isolated_commerce_database_required')
        self.engine = create_async_engine(url, hide_parameters=True, echo=False)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            for model in (XYAccount, XYOrder, Card, DeliveryIntent):
                await conn.run_sync(lambda sync, m=model: m.__table__.create(sync, checkfirst=True))
        self.processes, self.scenarios, self.results = [], {}, []
        app = web.Application()
        app.router.add_route('*', '/{identity}/{phase}', self.external)
        self.server = web.AppRunner(app)
        await self.server.setup()
        site = web.TCPSite(self.server, '127.0.0.1', 0)
        await site.start()
        self.base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        api = FastAPI()
        api.include_router(router, prefix='/api/v1/orders')
        async def database():
            async with self.sessions() as session:
                yield session
        api.dependency_overrides[deps.get_db_session] = database
        api.dependency_overrides[deps.get_current_active_user] = lambda: SimpleNamespace(id=777)
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://fixture')

    async def asyncTearDown(self):
        for process in self.processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        for case in self.scenarios.values():
            case['release'].set()
        await self.api.aclose()
        await self.server.cleanup()
        await self.engine.dispose()
        directory = os.environ.get('XYMB_VERIFY_OUTPUT')
        if directory:
            Path(directory, self._testMethodName + '.json').write_text(json.dumps(self.results, indent=2) + '\n')

    async def external(self, request):
        case = self.scenarios[request.match_info['identity']]
        phase = request.match_info['phase']
        if phase == 'query':
            case['queries'] += 1
            if not case['purchases']:
                return web.json_response({'data': ''})
            return web.json_response({'data': 'PURCHASED-CARD'})
        body = await request.json()
        case['calls'].append(phase)
        if phase == 'purchase':
            case['purchases'].append(request.headers['Idempotency-Key'])
        if phase == 'content':
            case['contents'].append(body['text'])
        if case['phase'] == phase and case['point'] == 'in_flight':
            case['accepted'].set()
            await case['release'].wait()
        if phase == 'purchase':
            return web.json_response({'data': 'PURCHASED-CARD'})
        if phase == 'content':
            return web.json_response({'delivery_state': 'confirmed'})
        if case.get('fail_confirmation'):
            case['fail_confirmation'] = False
            return web.json_response({'success': False, 'outcome': 'not_sent'})
        return web.json_response({'success': True})

    async def new_case(self, phase, point, *, fail_confirmation=False):
        identity = 'crash-' + uuid4().hex
        case = dict(identity=identity, phase=phase, point=point, calls=[], contents=[], purchases=[], queries=0,
                    accepted=asyncio.Event(), release=asyncio.Event(), fail_confirmation=fail_confirmation)
        self.scenarios[identity] = case
        async with self.sessions() as session:
            account = XYAccount(owner_id=777, account_id=identity, unb=identity, cookie='fixture',
                                login_method='cookie', status='active')
            card = Card(user_id=777, name='crash-fixture', type='api' if phase == 'purchase' else 'data',
                        data_content='ONE\nSPARE', api_config=json.dumps({'url': self.base + '/' + identity + '/purchase',
                        'query_url': self.base + '/' + identity + '/query', 'method': 'POST', 'response_field': 'data',
                        'idempotency_header': 'Idempotency-Key', 'params': {'quantity': '{quantity}'}}))
            order = XYOrder(owner_id=777, account_id=identity, order_no='order', status='pending_ship', quantity=1)
            session.add_all([account, card, order])
            await session.flush()
            case['card_id'] = card.id
            await session.commit()
        return case

    async def start(self, case, *, crash=False, now=None):
        command = [str(ROOT / '.venv/bin/python'), str(ROOT / 'tests/commerce/process_fixture.py'),
            '--account', case['identity'], '--order', 'order', '--card', str(case['card_id']),
            '--url', self.base + '/' + case['identity']]
        if crash:
            command += ['--phase', case['phase'], '--point', case['point']]
        if now is not None:
            command += ['--now', str(now)]
        process = await asyncio.create_subprocess_exec(*command, cwd=ROOT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, 'PYTHONPATH': 'backend-web:.', 'SQL_ECHO': 'false'})
        self.processes.append(process)
        return process

    async def checkpoint(self, process):
        line = await asyncio.wait_for(process.stdout.readline(), 15)
        self.assertTrue(line, 'worker stopped before its external boundary')
        result = json.loads(line)
        self.assertNotIn('error', result)
        return result

    async def view(self, case):
        result = await self.api.get('/api/v1/orders/commerce/intents', params={'account_id': case['identity']})
        self.assertEqual(result.status_code, 200)
        rows = result.json()['data']
        self.assertEqual(len(rows), 1, 'replayed order created a second fulfillment')
        case['intent_id'] = rows[0]['id']
        return rows[0]

    async def restart(self, case, *, now=None):
        process = await self.start(case, now=now)
        stdout, _ = await asyncio.wait_for(process.communicate(), 15)
        self.assertEqual(process.returncode, 0, stdout.decode())
        result = json.loads(stdout)
        self.assertTrue(result['done'])
        self.assertEqual(result['intent_id'], case['intent_id'])
        return await self.view(case)

    async def crash_case(self, phase, point):
        case = await self.new_case(phase, point)
        process = await self.start(case, crash=True)
        if point == 'in_flight':
            await asyncio.wait_for(case['accepted'].wait(), 15)
        else:
            marker = await self.checkpoint(process)
            self.assertEqual((marker['phase'], marker['checkpoint']), (phase, point))
        process.kill()
        await asyncio.wait_for(process.wait(), 5)
        self.assertEqual(process.returncode, -9)
        case['release'].set()
        row = await self.view(case)
        before = list(case['calls'])
        row = await self.restart(case)
        self.assertEqual(case['calls'], before, 'restart repeated an uncertain external side effect')
        expected = {'purchase': 'purchasing', 'content': 'sending', 'confirm': 'confirming'}[phase]
        self.assertEqual(row['confirm_state'] if phase == 'confirm' else row['content_state'], expected)
        self.results.append({'phase': phase, 'point': point, 'signal': 9, 'uncertain_state': expected,
                             'requests_before': before, 'requests_after_restart': list(case['calls'])})
        return case

    async def reconcile(self, case, phase, result):
        response = await self.api.post(f'/api/v1/orders/commerce/intents/{case["intent_id"]}/reconcile',
            json={'phase': phase, 'result': result, 'evidence': 'fixture-receipt:observed-boundary'})
        self.assertEqual(response.status_code, 200, response.text)

    async def test_sigkill_purchase_before_during_and_after_response_never_charges_twice(self):
        for point in ('before_request', 'in_flight', 'after_response'):
            with self.subTest(point=point):
                case = await self.crash_case('purchase', point)
                self.assertEqual(len(case['purchases']), int(point != 'before_request'))
                if case['purchases']:
                    self.assertEqual(case['purchases'], [case['intent_id']])
                    await DeliveryExecution(self.sessions).verify_supplier(case['intent_id'], 777, query=query_card)
                    final = await self.restart(case)
                    self.assertEqual((final['content_state'], final['confirm_state']), ('confirmed', 'confirmed'))
                    self.assertEqual(case['purchases'], [case['intent_id']])
                    self.assertEqual(case['contents'], ['PURCHASED-CARD'])
                    self.assertEqual(case['queries'], 1)
                else:
                    self.assertEqual(case['contents'], [])

    async def test_sigkill_send_before_during_and_after_response_keeps_inventory_reserved(self):
        for point in ('before_request', 'in_flight', 'after_response'):
            with self.subTest(point=point):
                case = await self.crash_case('content', point)
                async with self.sessions() as session:
                    self.assertEqual((await session.get(Card, case['card_id'])).data_content, 'SPARE')
                sent = point != 'before_request'
                await self.reconcile(case, 'content', 'confirmed' if sent else 'not_sent')
                final = await self.restart(case)
                self.assertEqual(case['contents'], ['ONE'] if sent else [])
                self.assertEqual(final['content_state'], 'confirmed' if sent else 'not_sent')
                async with self.sessions() as session:
                    card = await session.get(Card, case['card_id'])
                    self.assertEqual(card.data_content, 'SPARE' if sent else 'ONE\nSPARE')
                    self.assertEqual(card.delivery_count or 0, int(sent))

    async def test_sigkill_confirmation_at_each_boundary_does_not_send_cards_again(self):
        for point in ('before_request', 'in_flight', 'after_response'):
            with self.subTest(point=point):
                case = await self.crash_case('confirm', point)
                self.assertEqual(case['contents'], ['ONE'])
                await self.reconcile(case, 'confirm', 'not_sent' if point == 'before_request' else 'confirmed')
                final = await self.restart(case)
                self.assertEqual((final['content_state'], final['confirm_state']), ('confirmed', 'confirmed'))
                self.assertEqual(case['contents'], ['ONE'])
                self.assertEqual(case['calls'].count('confirm'), 1)

    async def test_sigkill_after_reservation_replays_same_intent_and_stock_once(self):
        case = await self.new_case('reservation', 'persisted')
        process = await self.start(case, crash=True)
        marker = await self.checkpoint(process)
        self.assertEqual(marker['checkpoint'], 'persisted')
        process.kill()
        await process.wait()
        self.assertEqual(process.returncode, -9)
        row = await self.view(case)
        self.assertEqual(row['content_state'], 'reserved')
        self.assertEqual(case['calls'], [])
        final = await self.restart(case)
        self.assertEqual((final['content_state'], final['confirm_state']), ('confirmed', 'confirmed'))
        self.assertEqual(case['contents'], ['ONE'])
        async with self.sessions() as session:
            self.assertEqual((await session.get(Card, case['card_id'])).data_content, 'SPARE')
        self.results.append({'phase': 'reservation', 'point': 'persisted', 'signal': 9, 'requests_after_restart': case['calls']})

    async def test_sigkill_after_failed_confirmation_only_retries_platform_confirmation(self):
        case = await self.new_case('done', 'persisted', fail_confirmation=True)
        process = await self.start(case, crash=True, now=100)
        await self.checkpoint(process)
        process.kill()
        await process.wait()
        row = await self.view(case)
        self.assertEqual((row['content_state'], row['confirm_state'], row['next_retry_at']), ('confirmed', 'pending', 160))
        await self.restart(case, now=159)
        self.assertEqual(case['calls'].count('confirm'), 1)
        final = await self.restart(case, now=160)
        self.assertEqual(final['confirm_state'], 'confirmed')
        self.assertEqual(case['contents'], ['ONE'])
        self.assertEqual(case['calls'].count('confirm'), 2)
        self.results.append({'phase': 'confirm', 'point': 'failed_persisted', 'signal': 9, 'requests_after_restart': case['calls']})
