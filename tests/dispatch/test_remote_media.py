"""S2: real CONNECT proxy and origin; DNS is the only remote-network substitute."""
import asyncio
import base64
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from aiohttp import web
import test_flow as fixtures
from common.models.xy_account import XYAccount
from common.services import account_policy
from common.services.account_dispatch import ExecutionContext

PUBLIC_IP = '93.184.216.34'


class RemoteMediaTests(unittest.IsolatedAsyncioTestCase):
    request = fixtures.FlowTests.request

    async def asyncSetUp(self):
        await fixtures.FlowTests.asyncSetUp(self)
        self.addAsyncCleanup(fixtures.FlowTests.asyncTearDown, self)
        self.origin_calls = []
        self.proxy_calls = []
        self.connections = set()
        self.status, self.body, self.headers = 200, b'media-fixture', {}
        async def origin(request):
            self.origin_calls.append({'path':request.path, 'headers':dict(request.headers)})
            return web.Response(status=self.status, body=self.body, headers=self.headers)
        app = web.Application(); app.router.add_get('/{path:.*}', origin)
        self.runner = web.AppRunner(app); await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0); await site.start()
        self.origin_port = site._server.sockets[0].getsockname()[1]
        self.proxy = await asyncio.start_server(self.tunnel, '127.0.0.1', 0)
        async with self.sessions() as db:
            account = await db.get(XYAccount, 1)
            account.proxy_type, account.proxy_host = 'http', '127.0.0.1'
            account.proxy_port = self.proxy.sockets[0].getsockname()[1]
            account.proxy_user, account.proxy_pass = 'media-user', 'media-pass'
            state = account_policy.snapshot(account)
            state['config_values']['risk'] = {'min_interval_seconds': .001}
            account_policy.store(account, state); await db.commit()
            binding = account_policy.proxy_url({key:getattr(account,key) for key in (
                'proxy_type','proxy_host','proxy_port','proxy_user','proxy_pass')})
            self.live._get_proxy_url = lambda: binding
        self.context = ExecutionContext(self.request('get_conversations'), self.live, self.dispatcher.store, self.budget)

    async def tunnel(self, reader, writer):
        current = asyncio.current_task(); self.connections.add(current); upstream = None
        try:
            lines = (await reader.readuntil(b'\r\n\r\n')).decode('latin1').split('\r\n')
            self.proxy_calls.append(lines[0])
            headers = dict(line.split(': ', 1) for line in lines[1:] if ': ' in line)
            expected = 'Basic ' + base64.b64encode(b'media-user:media-pass').decode()
            if headers.get('Proxy-Authorization') != expected:
                writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n')
                await writer.drain(); return
            if lines[0] != f'CONNECT {PUBLIC_IP}:80 HTTP/1.1':
                writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                await writer.drain(); return
            source, upstream = await asyncio.open_connection('127.0.0.1', self.origin_port)
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n'); await writer.drain()
            async def relay(source, target):
                while data := await source.read(65536):
                    target.write(data); await target.drain()
            relays = [asyncio.create_task(relay(reader, upstream)), asyncio.create_task(relay(source, writer))]
            try: await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in relays: task.cancel()
                await asyncio.gather(*relays, return_exceptions=True)
        except (ConnectionError, asyncio.IncompleteReadError): pass
        finally:
            if upstream: upstream.close(); await upstream.wait_closed()
            writer.close()
            try: await writer.wait_closed()
            except ConnectionError: pass
            self.connections.discard(current)

    async def asyncTearDown(self):
        self.proxy.close(); await self.proxy.wait_closed()
        for task in list(self.connections): task.cancel()
        await asyncio.gather(*list(self.connections), return_exceptions=True)
        await self.runner.cleanup()

    def dns(self, ip=PUBLIC_IP):
        return patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80))])

    async def test_download_uses_fixed_proxy_and_pinned_public_address_without_account_secrets(self):
        from common.services.remote_media import read_remote_media
        with self.dns(), patch.dict('os.environ', {'HTTP_PROXY':'http://127.0.0.1:1'}):
            content, _ = await read_remote_media(self.context, 'http://media.fixture.example/image', max_bytes=100)
        self.assertEqual(content, b'media-fixture')
        self.assertEqual(self.proxy_calls, [f'CONNECT {PUBLIC_IP}:80 HTTP/1.1'])
        self.assertEqual(self.origin_calls[0]['headers']['Host'], 'media.fixture.example')
        self.assertFalse(self.origin_calls[0]['headers'].get('Cookie'))
        self.assertNotIn('Authorization', self.origin_calls[0]['headers'])
        self.assertNotIn('Proxy-Authorization', self.origin_calls[0]['headers'])
        self.assertEqual(self.context.external_count, 1)

    async def test_private_dns_and_redirect_are_stopped_before_follow_up_request(self):
        from common.services.remote_media import read_remote_media
        from common.services.account_dispatch import PlatformFailure
        for url in ('http://127.0.0.1/secret', 'http://169.254.169.254/credentials', 'http://[::1]/'):
            with self.assertRaises(PlatformFailure):
                await read_remote_media(self.context, url, max_bytes=100)
        with self.dns('10.0.0.1'), self.assertRaises(PlatformFailure):
            await read_remote_media(self.context, 'http://media.fixture.example/x', max_bytes=100)
        self.assertEqual(self.proxy_calls, [])
        self.status, self.headers = 302, {'Location':'http://127.0.0.1/secret'}
        with self.dns(), self.assertRaises(PlatformFailure):
            await read_remote_media(self.context, 'http://media.fixture.example/image', max_bytes=100)
        self.assertEqual(len(self.proxy_calls), 1)
        self.assertEqual([call['path'] for call in self.origin_calls], ['/image'])

    async def test_oversize_and_rate_limited_downloads_are_not_retried(self):
        from common.services.remote_media import read_remote_media
        from common.services.account_dispatch import PlatformFailure
        self.body = b'x' * 101
        with self.dns(), self.assertRaises(PlatformFailure) as failure:
            await read_remote_media(self.context, 'http://media.fixture.example/image', max_bytes=100)
        self.assertEqual(failure.exception.code, 'remote_media_too_large')
        self.status, self.body, self.headers = 429, b'wait', {'Retry-After':'91'}
        with self.dns(), self.assertRaises(PlatformFailure) as failure:
            await read_remote_media(self.context, 'http://media.fixture.example/image', max_bytes=100)
        self.assertEqual(failure.exception.retry_after, 91)
        self.assertEqual(len(self.proxy_calls), 2)
        self.assertEqual(self.context.external_count, 2)

    async def test_bad_proxy_credentials_keep_origin_untouched(self):
        from common.services.remote_media import read_remote_media
        from common.services.account_dispatch import PlatformFailure
        async with self.sessions() as db:
            account = await db.get(XYAccount, 1); account.proxy_pass = 'incorrect'; await db.commit()
        with self.dns(), self.assertRaises(PlatformFailure):
            await read_remote_media(self.context, 'http://media.fixture.example/image', max_bytes=100)
        self.assertEqual(self.origin_calls, [])
        self.assertEqual(len(self.proxy_calls), 1)

    async def test_image_url_dispatch_fetches_then_uploads_once_on_the_executor(self):
        from io import BytesIO
        from PIL import Image
        from test_video_upload import MediaEndpoints
        image = BytesIO(); Image.new('RGB', (2, 3), 'blue').save(image, 'PNG'); self.body = image.getvalue()
        self.live.session = MediaEndpoints()
        request = self.request('upload_image', url='http://media.fixture.example/image', purpose='product')
        with self.dns():
            result = await self.dispatcher.execute(request)
            self.assertEqual(result.status, 'confirmed', result)
            self.assertEqual(result.result['width'], 2)
            self.assertEqual(result.result['height'], 3)
            repeated = await self.dispatcher.execute(request)
        self.assertEqual(repeated.model_dump(), result.model_dump())
        self.assertEqual(len(self.origin_calls), 1)
        self.assertEqual(len(self.live.session.calls), 1)

    async def test_video_url_dispatch_runs_fetch_and_all_upload_phases_without_replay(self):
        from pathlib import Path
        import cv2
        import numpy as np
        from test_video_upload import MediaEndpoints
        file = Path(self.tmp.name) / 'remote.mp4'
        writer = cv2.VideoWriter(str(file), cv2.VideoWriter_fourcc(*'mp4v'), 1, (16,16))
        writer.write(np.zeros((16,16,3), dtype=np.uint8)); writer.release()
        self.body = file.read_bytes()
        self.live.session = MediaEndpoints()
        request = self.request('upload_video', url='http://media.fixture.example/remote.mp4', name='remote.mp4')
        with self.dns():
            result = await self.dispatcher.execute(request)
            repeated = await self.dispatcher.execute(request)
        self.assertEqual(result.status, 'confirmed', result)
        self.assertEqual(result.result['video']['mediaCloudFileId'], 'file-fixture')
        self.assertEqual(repeated.model_dump(), result.model_dump())
        self.assertEqual(len(self.origin_calls), 1)
        self.assertEqual(len(self.live.session.calls), 5)
