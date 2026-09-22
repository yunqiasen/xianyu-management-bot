"""S2: actual HTTP and message clients traverse an authenticated TLS CONNECT proxy."""
import asyncio
import base64
import ipaddress
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


class TLSProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
        now=datetime.now(timezone.utc)
        cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
              .not_valid_after(now+timedelta(days=1))
              .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
              .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
        certfile=Path(self.tmp.name)/'ca.pem'; keyfile=Path(self.tmp.name)/'key.pem'
        certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        keyfile.chmod(0o600)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(certfile,keyfile)
        env=patch.dict(os.environ,{'SSL_CERT_FILE':str(certfile), 'HTTPS_PROXY':'http://127.0.0.1:1'})
        env.start(); self.addCleanup(env.stop)
        self.target_calls=[]; self.proxy_calls=[]; self.connections=set()
        async def target(request):
            self.target_calls.append(request.path)
            if request.path=='/ws':
                socket=web.WebSocketResponse(); await socket.prepare(request)
                async for message in socket:
                    await socket.send_str(message.data)
                return socket
            return web.Response(text='through fixed proxy')
        app=web.Application();app.router.add_get('/{path:.*}',target)
        self.runner=web.AppRunner(app);await self.runner.setup()
        site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        self.target_port=site._server.sockets[0].getsockname()[1]
        self.proxy=await asyncio.start_server(self.tunnel,'127.0.0.1',0,ssl=context)
        self.proxy_port=self.proxy.sockets[0].getsockname()[1]
        self.live=None

    async def tunnel(self, reader, writer):
        task=asyncio.current_task();self.connections.add(task); upstream=None
        try:
            data=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
            lines=data.decode('latin1').split('\r\n')
            self.proxy_calls.append(lines[0])
            expected='Basic '+base64.b64encode(b'fixture-user:fixture-pass').decode()
            headers=dict(line.split(': ',1) for line in lines[1:] if ': ' in line)
            if headers.get('Proxy-Authorization')!=expected:
                writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n');await writer.drain();return
            if lines[0]!=f'CONNECT 127.0.0.1:{self.target_port} HTTP/1.1':
                writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n');await writer.drain();return
            upstream_reader,upstream=await asyncio.open_connection('127.0.0.1',self.target_port)
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');await writer.drain()
            async def relay(source,dest):
                while content:=await source.read(65536):
                    dest.write(content);await dest.drain()
            relays=[asyncio.create_task(relay(reader,upstream)),asyncio.create_task(relay(upstream_reader,writer))]
            try: await asyncio.wait(relays,return_when=asyncio.FIRST_COMPLETED)
            finally:
                for relay_task in relays:relay_task.cancel()
                await asyncio.gather(*relays,return_exceptions=True)
        except (ConnectionError,asyncio.IncompleteReadError,asyncio.TimeoutError):pass
        finally:
            if upstream:upstream.close();await upstream.wait_closed()
            writer.close()
            try:await writer.wait_closed()
            except ConnectionError:pass
            self.connections.discard(task)

    def clients(self, password='fixture-pass'):
        from app.services.xianyu.xianyu_async import XianyuAsync
        from app.services.xianyu.connection_manager import ConnectionManager
        live=object.__new__(XianyuAsync)
        live.proxy_config={'proxy_type':'https','proxy_host':'127.0.0.1','proxy_port':self.proxy_port,
                           'proxy_user':'fixture-user','proxy_pass':password}
        live.cookie_id='proxy-fixture';live.cookies_str='unb=101';live.session=None
        live.base_url=f'ws://127.0.0.1:{self.target_port}/ws'
        live._account_runtime=SimpleNamespace(check=AsyncMock(),before_business_request=AsyncMock(),pause=AsyncMock())
        manager=object.__new__(ConnectionManager);manager.xianyu=live;manager.cookie_id=live.cookie_id
        self.live=live
        return live,manager

    async def asyncTearDown(self):
        if self.live:await self.live.close_session()
        self.proxy.close();await self.proxy.wait_closed()
        for task in list(self.connections):task.cancel()
        await asyncio.gather(*list(self.connections),return_exceptions=True)
        await self.runner.cleanup()

    async def test_http_and_websocket_use_same_authenticated_https_proxy(self):
        live,manager=self.clients()
        await live.create_session()
        async with live.session.get(f'http://127.0.0.1:{self.target_port}/http') as response:
            self.assertEqual(await response.text(),'through fixed proxy')
        connection=await manager.create_websocket_connection({})
        async with connection as socket:
            await socket.send('fixture');self.assertEqual(await socket.recv(),'fixture')
        self.assertEqual(self.target_calls,['/http','/ws'])
        self.assertEqual(len(self.proxy_calls),2)

    async def test_auth_failure_never_falls_back_to_direct(self):
        live,manager=self.clients('incorrect')
        await live.create_session()
        with self.assertRaises(Exception):
            async with live.session.get(f'http://127.0.0.1:{self.target_port}/http'):pass
        with self.assertRaises(Exception):
            connection=await manager.create_websocket_connection({})
            async with connection:pass
        self.assertEqual(self.target_calls,[])
        self.assertEqual(len(self.proxy_calls),2)
