"""S1/S2/S3: real auth routes, SMTP wire messages and a fresh MySQL database."""
import asyncio
from email import policy
from email.parser import BytesParser
from hashlib import sha256
import re
import socketserver
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import deps
from app.api.routes import auth, captcha, system_settings
from app.services.auth import AuthService
from app.services import email_service
from common.db.fork_schema import upgrade_fork_schema
from common.models.system_setting import SystemSetting
from common.models.user import User, UserRole
from tools.verification.run import commerce_fixture


class MailServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class MailHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'220 fixture SMTP\r\n')
        while line := self.rfile.readline():
            command = line.split(b' ', 1)[0].strip().upper()
            if command in {b'EHLO', b'HELO'}:
                self.wfile.write(b'250-fixture\r\n250 AUTH PLAIN\r\n')
            elif command == b'AUTH':
                self.wfile.write(b'235 authenticated\r\n')
            elif command == b'DATA':
                self.wfile.write(b'354 send message\r\n')
                parts = []
                while (part := self.rfile.readline()) not in (b'.\r\n', b''):
                    parts.append(part[1:] if part.startswith(b'..') else part)
                self.server.messages.append(b''.join(parts))
                self.wfile.write(b'250 accepted\r\n')
            elif command == b'QUIT':
                self.wfile.write(b'221 bye\r\n')
                break
            else:
                self.wfile.write(b'250 ok\r\n')


class AuthLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = commerce_fixture()
        self.engine = create_async_engine(self.fixture.__enter__(), hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.mail = MailServer(('127.0.0.1', 0), MailHandler)
        self.mail.messages = []
        self.thread = threading.Thread(target=self.mail.serve_forever, daemon=True)
        self.thread.start()
        self.email = 'new-' + uuid4().hex[:12] + '@example.com'
        self.now = time.time()
        async with self.engine.begin() as connection:
            await connection.run_sync(User.__table__.create)
            await connection.run_sync(SystemSetting.__table__.create)
            await connection.run_sync(upgrade_fork_schema)
        async with self.sessions() as session:
            admin = User(id=1, username='admin-fixture', email='admin@example.com',
                password_hash=sha256(b'fixture-password').hexdigest(), role=UserRole.ADMIN)
            session.add(admin)
            settings = {'login_captcha_enabled': 'false', 'registration_enabled': 'true',
                        'smtp_server': '127.0.0.1', 'smtp_port': str(self.mail.server_address[1]),
                        'smtp_user': 'sender@example.com', 'smtp_password': 'smtp-fixture'}
            session.add_all([SystemSetting(key=k, value=v) for k, v in settings.items()])
            await session.commit()
            self.admin_headers = {'Authorization': 'Bearer ' + AuthService(session).create_access_token(admin)}
        self.app = FastAPI()
        self.app.include_router(auth.router, prefix='/api/v1/auth')
        self.app.include_router(captcha.router, prefix='/api/v1')
        self.app.include_router(system_settings.router, prefix='/api/v1/system-settings')
        async def database():
            async with self.sessions() as session:
                yield session
        self.app.dependency_overrides[deps.get_db_session] = database
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://fixture')
        self.database_patch = patch.object(email_service, 'async_session_maker', self.sessions)
        self.clock_patch = patch.object(captcha, 'time', SimpleNamespace(time=lambda: self.now))
        self.database_patch.start()
        self.clock_patch.start()

    async def asyncTearDown(self):
        self.clock_patch.stop()
        self.database_patch.stop()
        captcha.email_code_store.pop(self.email, None)
        await self.client.aclose()
        await asyncio.to_thread(self.mail.shutdown)
        self.mail.server_close()
        self.thread.join(timeout=2)
        await self.engine.dispose()
        self.fixture.__exit__(None, None, None)

    async def code(self, kind):
        response = await self.client.post('/api/v1/captcha/send-email-code', json={'email': self.email, 'type': kind})
        self.assertTrue(response.json()['success'], response.text)
        message = BytesParser(policy=policy.default).parsebytes(self.mail.messages[-1])
        self.assertEqual(str(message['To']), self.email)
        body = message.get_body(preferencelist=('plain',)).get_content()
        return re.search(r'(?<!\d)\d{6}(?!\d)', body).group(0)

    async def register(self, code, username='new-user'):
        return await self.client.post('/api/v1/auth/register', json={
            'username': username, 'email': self.email, 'password': 'first-password', 'verification_code': code})

    async def login(self, password):
        return (await self.client.post('/api/v1/auth/login', json={'email': self.email, 'password': password})).json()

    async def authenticated(self, token):
        response = await self.client.get('/api/v1/auth/verify', headers={'Authorization': 'Bearer ' + token})
        return response.json()['authenticated']

    async def test_register_refresh_logout_reset_and_email_login_are_one_time(self):
        code = await self.code('register')
        wrong = '000000' if code != '000000' else '999999'
        self.assertEqual((await self.register(wrong)).status_code, 400)
        self.assertEqual((await self.register(code)).status_code, 201)
        first = await self.login('first-password')
        self.assertTrue(first['success'])
        refreshed = (await self.client.post('/api/v1/auth/refresh',
            headers={'Authorization': 'Bearer ' + first['refresh_token']})).json()
        self.assertTrue(refreshed['success'])
        self.assertFalse(await self.authenticated(first['refresh_token']))
        self.assertTrue(await self.authenticated(refreshed['token']))
        await self.client.post('/api/v1/auth/logout', headers={'Authorization': 'Bearer ' + refreshed['token']})
        self.assertFalse(await self.authenticated(first['token']))
        self.assertFalse((await self.client.post('/api/v1/auth/refresh',
            headers={'Authorization': 'Bearer ' + first['refresh_token']})).json()['success'])
        second = await self.login('first-password')
        code = await self.code('reset_password')
        short = await self.client.post('/api/v1/auth/reset-password', json={
            'email': self.email, 'verification_code': code, 'new_password': 'tiny'})
        self.assertFalse(short.json()['success'])
        reset = await self.client.post('/api/v1/auth/reset-password', json={
            'email': self.email, 'verification_code': code, 'new_password': 'second-password'})
        self.assertTrue(reset.json()['success'], reset.text)
        self.assertFalse(await self.authenticated(second['token']))
        self.assertFalse((await self.login('first-password'))['success'])
        self.assertTrue((await self.login('second-password'))['success'])
        code = await self.code('login')
        for expected in (True, False):
            response = await self.client.post('/api/v1/auth/login', json={'email': self.email, 'verification_code': code})
            self.assertIs(response.json()['success'], expected, response.text)

    async def test_expired_code_and_registration_switch_block_server_side(self):
        expired = await self.code('register')
        self.now += 301
        self.assertEqual((await self.register(expired)).status_code, 400)
        valid = await self.code('register')
        changed = await self.client.put('/api/v1/system-settings/registration_enabled',
            json={'value': 'false'}, headers=self.admin_headers)
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual((await self.register(valid)).status_code, 403)
        self.now += 61
        count = len(self.mail.messages)
        blocked = await self.client.post('/api/v1/captcha/send-email-code', json={'email': self.email, 'type': 'register'})
        self.assertFalse(blocked.json()['success'])
        self.assertEqual(len(self.mail.messages), count)
        await self.client.put('/api/v1/system-settings/registration_enabled',
            json={'value': 'true'}, headers=self.admin_headers)
        self.assertEqual((await self.register(valid)).status_code, 201)
