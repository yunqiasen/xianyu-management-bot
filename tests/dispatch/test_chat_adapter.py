import unittest
import importlib.util
from pathlib import Path
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
import test_flow as fixtures
from common.services.account_dispatch import AccountDispatchClient


class ChatAdapterTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FlowTests.asyncSetUp
    asyncTearDown = fixtures.FlowTests.asyncTearDown
    request = fixtures.FlowTests.request

    async def test_chat_frontend_uses_existing_socket_without_second_login(self):
        await self.runtime.record_connection(True, verified=True)
        path=Path(__file__).resolve().parents[2]/'backend-web/app/services/chat_new/executor_client.py'
        spec=importlib.util.spec_from_file_location('chat_executor_fixture',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        from app.api.routes.account_operations import router,get_account_dispatcher
        from app.api.deps import require_internal_auth
        app=FastAPI();app.include_router(router)
        app.dependency_overrides[get_account_dispatcher]=lambda:self.dispatcher
        app.dependency_overrides[require_internal_auth]=lambda:None
        async with AsyncClient(transport=ASGITransport(app=app)) as http:
            rpc=AccountDispatchClient('http://fixture','fixture-'*8,http=http)
            client=module.ExecutorImClient('fixture','STALE_COOKIE',account_row_id=1,
                                           rpc=rpc,sessions=self.sessions)
            self.assertTrue(await client.connect())
            self.assertEqual(self.socket.sent, [])  # Status checks consume no platform budget.
            self.assertEqual(client.myid,'101');self.assertEqual(client.cookies_str,'')
            self.redis.now+=2000
            result=await client.send_text_message('chat','buyer','hello',request_id='manual-1')
            self.assertEqual(result['response']['code'],200)
            await client.send_text_message('chat','buyer','hello',request_id='manual-1')
            self.assertEqual(len(self.socket.sent),1)  # one user message, no connection probe
            await client.disconnect()
            self.assertFalse(client.is_connected)
            await self.runtime.lease.check()
            self.assertEqual(self.redis.generation,1)

    async def test_event_subscription_can_restart_without_replaying_old_messages(self):
        import asyncio
        from common.services.reply_state import ReplyState
        from app.api.routes.account_operations import router, get_account_dispatcher
        from app.api.deps import require_internal_auth
        path=Path(__file__).resolve().parents[2]/'backend-web/app/services/chat_new/executor_client.py'
        spec=importlib.util.spec_from_file_location('chat_event_fixture',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        app=FastAPI();app.include_router(router)
        app.dependency_overrides[get_account_dispatcher]=lambda:self.dispatcher
        app.dependency_overrides[require_internal_auth]=lambda:None
        await self.runtime.record_connection(True, verified=True)
        async with AsyncClient(transport=ASGITransport(app=app)) as http:
            client=module.ExecutorImClient('fixture',account_row_id=1,
                rpc=AccountDispatchClient('http://fixture','fixture-'*8,http=http),sessions=self.sessions)
            self.assertTrue(await client.connect())
            first, second = asyncio.Event(), asyncio.Event()
            seen=[]
            async def first_callback(event): first.set()
            async def second_callback(event):
                seen.append(event)
                second.set()
            store=ReplyState(self.sessions)
            try:
                client.add_push_callback(first_callback)
                await store.record_message('fixture','chat','event-1','user','first')
                await asyncio.wait_for(first.wait(), 2)
                client.remove_push_callback(first_callback)
                await asyncio.sleep(1.1)
                client.add_push_callback(second_callback)
                await store.record_message('fixture','chat','event-2','user','second')
                await asyncio.wait_for(second.wait(), 2)
                self.assertEqual(len(seen), 1)
                self.assertEqual(seen[0]['message']['text'], 'second')
                self.assertEqual(self.socket.sent, [])
            finally:
                await client.disconnect()
