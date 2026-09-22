"""D13 / S2: legacy encoding and session-readiness intent at current public clients."""
import json
import unittest
from unittest.mock import patch
from aiohttp import web
from common.services import im_token_api
from common.services.account_credentials import validate_credentials, CredentialRejected


class CredentialGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.response = {'ret': ['SUCCESS::调用成功'], 'data': {'accessToken': 'fixture-token'}}
        async def endpoint(request):
            self.requests.append((dict(request.query), dict(await request.post()), request.content_type))
            return web.json_response(self.response)
        app = web.Application()
        app.router.add_post('/{tail:.*}', endpoint)
        self.server = web.AppRunner(app)
        await self.server.setup()
        site = web.TCPSite(self.server, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.endpoint = patch.object(im_token_api, 'IM_TOKEN_API_BASE_URL', f'http://127.0.0.1:{port}/h5')
        self.endpoint.start()

    async def asyncTearDown(self):
        self.endpoint.stop()
        await self.server.cleanup()

    async def test_query_and_form_are_encoded_once_and_device_identity_roundtrips(self):
        device = 'fixture+&=中文/?"\\'
        result = await im_token_api.request_im_token('unb=101; _m_h5_tk=fixture_123', device,
            proxy_config={'proxy_type': 'none'})
        self.assertEqual(im_token_api.extract_im_access_token(result.response_json), 'fixture-token')
        query, form, content_type = self.requests[0]
        self.assertEqual(query['appKey'], '34839810')
        self.assertRegex(query['sign'], r'^[a-f0-9]{32}$')
        self.assertEqual(set(form), {'data'})
        self.assertEqual(content_type, 'application/x-www-form-urlencoded')
        self.assertEqual(json.loads(form['data']), {'appKey': '444e9908a51d1cb236a27862abc769c9', 'deviceId': device})

    async def test_complete_looking_cookie_is_not_accepted_without_session_confirmation(self):
        self.response = {'ret': ['SUCCESS::调用成功'], 'data': {}}
        with self.assertRaises(CredentialRejected):
            await validate_credentials('unb=101; cookie2=fixture; _m_h5_tk=fixture_123; cna=fixture',
                '101', {'proxy_type': 'none'})
