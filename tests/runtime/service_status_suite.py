"""S1/S2: management API observes real HTTP health payloads from workers."""
import asyncio
import json
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend-web'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from app.api.routes import system_control
from app.api.deps import get_current_admin_user

class ServiceStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def respond(reader, writer):
            request = await reader.read(4096)
            body = json.dumps({'success': True, 'data': {'version': 'xianyu-management-bot+fixture', 'commit': 'fixture', 'database': 'connected', 'redis': 'connected', 'workers_enabled': False}}).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
            await writer.drain();writer.close();await writer.wait_closed()
        self.server = await asyncio.start_server(respond, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.original = (system_control.settings.service_port, system_control.settings.websocket_service_url, system_control.settings.scheduler_service_url)
        system_control.settings.service_port = port
        system_control.settings.websocket_service_url = f'http://127.0.0.1:{port}'
        system_control.settings.scheduler_service_url = f'http://127.0.0.1:{port}'
        app = FastAPI();app.include_router(system_control.router)
        app.dependency_overrides[get_current_admin_user] = lambda: object()
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url='http://test')

    async def asyncTearDown(self):
        system_control.settings.service_port, system_control.settings.websocket_service_url, system_control.settings.scheduler_service_url = self.original
        await self.client.aclose();self.server.close();await self.server.wait_closed()

    async def test_shows_worker_switch_and_source_not_just_open_port(self):
        response = await self.client.get('/system-control/status')
        self.assertEqual(response.status_code, 200)
        for item in response.json()['data']['services']:
            self.assertTrue(item['online'])
            self.assertEqual(item['database'], 'connected')
            self.assertEqual(item['redis'], 'connected')
            self.assertFalse(item['workers_enabled'])
            self.assertEqual(item['version'], 'xianyu-management-bot+fixture')

if __name__ == '__main__':unittest.main()
