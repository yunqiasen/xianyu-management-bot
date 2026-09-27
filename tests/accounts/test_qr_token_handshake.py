"""S1/S2: confirmed QR -> real credential HTTP handshake -> account save/start."""
import json
from hashlib import md5
from uuid import uuid4
from unittest.mock import patch

import httpx
from aiohttp import web
from fastapi import FastAPI
from sqlalchemy import select

from test_runtime import DatabaseCase
from common.models.user import User
from common.models.xy_account import XYAccount
from common.services import im_token_api
from common.utils.xianyu_utils import trans_cookies
from app.api import deps
from app.api.routes import qr_login
from app.core.config import get_settings
from app.core.http_client import get_http_client
from app.services.qr_login.manager import QRLoginSession


class QRTokenHandshakeTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.create_owner()
        self.requests = []
        self.starts = []
        self.responses = [
            (200, {'ret': ['FAIL_SYS_TOKEN_EMPTY::令牌为空'], 'data': {}},
             {'_m_h5_tk': 'signedfixture_9999999999999', '_m_h5_tk_enc': 'encryptedfixture'}),
            (200, {'ret': ['SUCCESS::调用成功'], 'data': {'accessToken': 'im-fixture'}},
             {'cookie2': 'renewedfixture'}),
        ]

        async def token(request):
            form = dict(await request.post())
            self.requests.append({'query': dict(request.query), 'form': form,
                                  'cookies': trans_cookies(request.headers.get('Cookie', ''))})
            status, body, cookies = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
            response = web.json_response(body, status=status)
            for key, value in cookies.items():
                response.set_cookie(key, value)
            return response

        async def start(request):
            self.starts.append(await request.json())
            return web.json_response({'success': True})

        server = web.Application()
        server.router.add_post('/h5/{tail:.*}', token)
        server.router.add_post('/internal/accounts/{account_id}/{action}', start)
        self.server = web.AppRunner(server)
        await self.server.setup()
        site = web.TCPSite(self.server, '127.0.0.1', 0)
        await site.start()
        self.base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        for setting in (patch.object(im_token_api, 'IM_TOKEN_API_BASE_URL', self.base + '/h5'),
                        patch.object(get_settings(), 'websocket_service_url', self.base),
                        patch.object(get_settings(), 'internal_api_token', 'fixture-' * 8)):
            setting.start()
            self.addCleanup(setting.stop)
        self.app = FastAPI()
        self.app.include_router(qr_login.router)

        async def owner():
            return User(id=7, username='owner')

        async def database():
            async with self.factory() as session:
                yield session

        self.app.dependency_overrides[deps.get_current_active_user] = owner
        self.app.dependency_overrides[deps.get_db_session] = database
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://fixture')
        self.session_id = 'qr-fixture-' + uuid4().hex
        self.qr = QRLoginSession(self.session_id)
        self.qr.status = 'success'
        self.qr.unb = '202'
        self.qr.cookies = {'unb': '202', 'cookie2': 'loginfixture'}
        qr_login.qr_login_manager.sessions[self.session_id] = self.qr
        qr_login.SESSION_OWNER[self.session_id] = 7

    async def asyncTearDown(self):
        for state in (qr_login.SESSION_OWNER, qr_login.SESSION_ACCOUNT,
                      qr_login.PROCESSED_SESSIONS, qr_login.SESSION_LOCKS,
                      qr_login.qr_login_manager.sessions):
            state.pop(self.session_id, None)
        await self.client.aclose()
        await get_http_client().close()
        await self.server.cleanup()
        await super().asyncTearDown()

    async def status(self):
        response = await self.client.get('/qr-login/status/' + self.session_id)
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_fresh_qr_initializes_signing_cookie_before_saving_and_starting(self):
        result = await self.status()
        self.assertTrue(result['success'], result.get('message'))
        self.assertEqual(result['data']['status'], 'success')
        self.assertTrue(result['data']['account_info']['is_new_account'])
        self.assertEqual(len(self.requests), 2)
        first, second = self.requests
        self.assertNotIn('_m_h5_tk', first['cookies'])
        self.assertEqual(second['cookies']['_m_h5_tk'], 'signedfixture_9999999999999')
        self.assertEqual(second['cookies']['_m_h5_tk_enc'], 'encryptedfixture')
        self.assertEqual(json.loads(first['form']['data'])['deviceId'],
                         json.loads(second['form']['data'])['deviceId'])
        signing_input = 'signedfixture&' + second['query']['t'] + '&34839810&' + second['form']['data']
        self.assertEqual(second['query']['sign'], md5(signing_input.encode()).hexdigest())
        self.assertEqual(len(self.starts), 1)
        forwarded = trans_cookies(self.starts[0]['cookie_value'])
        self.assertEqual(forwarded['_m_h5_tk'], 'signedfixture_9999999999999')
        self.assertEqual(forwarded['cookie2'], 'renewedfixture')
        async with self.factory() as db:
            account = await db.scalar(select(XYAccount).where(XYAccount.unb == '202'))
            self.assertIsNotNone(account)
            self.assertEqual(trans_cookies(account.cookie), forwarded)
        again = await self.status()
        self.assertEqual(again['data']['status'], 'already_processed')
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.starts), 1)
        self.assertNotIn('loginfixture', json.dumps(result))
        self.assertNotIn('im-fixture', json.dumps(result))

    async def test_unconfirmed_handshake_has_readable_failure_and_saves_nothing(self):
        self.responses = [self.responses[0]]
        result = await self.status()
        self.assertFalse(result['success'])
        self.assertEqual(result['data']['status'], 'failed')
        self.assertEqual(result['data']['reason'], 'token_initialization')
        self.assertEqual(result['message'], '登录凭据初始化失败，请重新扫码')
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(self.starts)
        async with self.factory() as db:
            self.assertIsNone(await db.scalar(select(XYAccount).where(XYAccount.unb == '202')))

    async def test_platform_stop_conditions_do_not_repeat_or_leak_upstream_details(self):
        cases = [
            (429, ['FAIL_SYS_TOKEN_EMPTY::令牌为空'], 'rate_limit'),
            (503, ['FAIL_SYS_TOKEN_EMPTY::令牌为空'], 'network'),
            (200, ['FAIL_SYS_SESSION_EXPIRED::Session过期'], 'invalid_credentials'),
            (200, ['FAIL_SYS_USER_VALIDATE::需要验证'], 'verification'),
            (200, ['FAIL_SYS_TRAFFIC_LIMIT::频繁'], 'rate_limit'),
            (200, ['FAIL::secretfixture'], 'unknown'),
            (200, None, 'unknown'),
            (200, 42, 'unknown'),
        ]
        for status, ret, reason in cases:
            with self.subTest(reason=reason, ret=ret):
                self.requests.clear()
                self.responses = [(status, {'ret': ret, 'data': {}},
                                   {'_m_h5_tk': 'signedfixture_9999999999999'})]
                result = await self.status()
                self.assertFalse(result['success'])
                self.assertEqual(result['data']['reason'], reason)
                self.assertNotIn('unknown', result['message'])
                self.assertNotIn('secretfixture', json.dumps(result))
                self.assertEqual(len(self.requests), 1)
                self.assertFalse(self.starts)
        async with self.factory() as db:
            self.assertIsNone(await db.scalar(select(XYAccount).where(XYAccount.unb == '202')))

    async def test_missing_or_unchanged_signing_cookie_never_triggers_a_blind_retry(self):
        self.qr.cookies['_m_h5_tk'] = 'existingfixture_1'
        for returned in ({}, {'_m_h5_tk': ''}, {'_m_h5_tk': 'existingfixture_1'}):
            with self.subTest(cookies=returned):
                self.requests.clear()
                self.responses = [(200, {'ret': ['FAIL_SYS_TOKEN_EXOIRED::令牌过期']}, returned)]
                result = await self.status()
                self.assertFalse(result['success'])
                self.assertEqual(result['data']['reason'], 'token_initialization')
                self.assertEqual(len(self.requests), 1)
                self.assertFalse(self.starts)

    async def test_response_identity_change_stops_before_retry_or_save(self):
        status, body, cookies = self.responses[0]
        self.responses[0] = (status, body, {**cookies, 'unb': '999'})
        result = await self.status()
        self.assertFalse(result['success'])
        self.assertIn('账号身份不一致', result['message'])
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.starts)

    async def test_automatic_validation_keeps_its_single_request_budget(self):
        from common.services.account_credentials import validate_credentials, CredentialRejected
        with self.assertRaises(CredentialRejected) as caught:
            await validate_credentials('unb=202; cookie2=loginfixture', '202', {'proxy_type': 'none'})
        self.assertEqual(caught.exception.reason, 'token_initialization')
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.starts)

    async def test_existing_disabled_account_keeps_profile_and_is_not_started(self):
        self.qr.unb = '101'
        self.qr.cookies['unb'] = '101'
        async with self.factory() as db:
            account = await db.get(XYAccount, 1)
            account.status = 'disabled'
            account.remark = 'keep-profile'
            await db.commit()
        result = await self.status()
        self.assertTrue(result['success'], result.get('message'))
        self.assertFalse(result['data']['account_info']['is_new_account'])
        self.assertEqual(result['data']['account_info']['account_id'], 'fixture')
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(self.starts)
        async with self.factory() as db:
            account = await db.get(XYAccount, 1)
            self.assertEqual(account.status, 'disabled')
            self.assertEqual(account.username, 'seller')
            self.assertEqual(account.login_password, 'secret')
            self.assertEqual(account.remark, 'keep-profile')
            self.assertEqual(trans_cookies(account.cookie)['_m_h5_tk'], 'signedfixture_9999999999999')

    async def test_simultaneous_polls_initialize_and_start_only_once(self):
        import asyncio
        results = await asyncio.gather(self.status(), self.status())
        self.assertEqual({row['data']['status'] for row in results}, {'success', 'already_processed'})
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.starts), 1)
