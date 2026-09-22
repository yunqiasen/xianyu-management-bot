"""Management chat adapter: reuse the account worker, never open a second platform socket."""
import asyncio
from uuid import uuid4
from sqlalchemy import select
from common.models.xy_account import XYAccount
from common.services.account_dispatch import AccountDispatchClient, DispatchError
from common.services.reply_state import MessageRejectedError, ReplyState


class ExecutorImClient:
    def __init__(self, account_id, cookies_str='', *, account_row_id=None, rpc=None, sessions=None):
        if sessions is None:
            from common.db.session import async_session_maker
            sessions = async_session_maker
        if rpc is None:
            from app.core.config import get_settings
            settings = get_settings()
            rpc = AccountDispatchClient(settings.websocket_service_url, settings.internal_api_token)
        self.account_id, self.account_row_id = account_id, account_row_id
        self.rpc, self.sessions = rpc, sessions
        self.owner_id = None
        self.myid, self.cookies_str = '', ''
        self._connected, self._push_cb_registered = False, False
        self._callbacks, self._poll_task = [], None
        self._event_cursor = 0

    @property
    def is_connected(self):
        return self._connected

    async def connect(self):
        async with self.sessions() as session:
            account = await session.scalar(select(XYAccount).where(XYAccount.account_id == self.account_id,
                                                   XYAccount.id == self.account_row_id))
            if account is None or account.status != 'active':
                return False
            self.owner_id, self.myid = account.owner_id, str(account.unb or '')
        status = await self.rpc.get_executor(self.owner_id, self.account_id)
        self._connected = status.ready
        return self._connected

    async def disconnect(self):
        self._connected = False
        if self._poll_task and self._poll_task is not asyncio.current_task():
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
        self._poll_task = None
        self._callbacks.clear()

    async def _call(self, command, payload, request_id=None):
        async with self.sessions() as session:
            result = await self.rpc.submit(owner_id=self.owner_id, account_id=self.account_id,
                request_id=request_id or 'chat-'+uuid4().hex, command=command, payload=payload, session=session)
        if result.status == 'confirmed':
            return result.result or {}
        if result.status == 'failed':
            raise MessageRejectedError(result.error_code or 'platform_rejected')
        raise DispatchError(result.error_code or 'result_unverified')

    async def get_conversations(self, start_timestamp=None, limit=20):
        return await self._call('get_conversations', dict(start_timestamp=start_timestamp, limit=limit))

    async def get_messages(self, cid, start_timestamp=None, limit=20):
        return await self._call('get_messages', dict(cid=cid, start_timestamp=start_timestamp, limit=limit))

    async def send_text_message(self, cid, to_user_id, text, request_id=None, item_id=''):
        return await self._call('send_text_message', dict(cid=cid, to_user_id=to_user_id, text=text,
                                  origin='manual', item_id=item_id), request_id)

    async def send_image_message(self, cid, to_user_id, image_url, width=800, height=600, request_id=None, item_id=''):
        return await self._call('send_image_message', dict(cid=cid, to_user_id=to_user_id,
            image_url=image_url, width=width, height=height, origin='manual', item_id=item_id), request_id)

    async def upload_image(self, path, request_id=None):
        from common.services.image_gateway import upload_account_image
        return await upload_account_image(self.account_id, self.owner_id, path, request_id=request_id,
                                           rpc=self.rpc, sessions=self.sessions)

    async def recall_message(self, message_id):
        result = await self._call('recall_message', {'message_id':message_id})
        return result['response']

    def add_push_callback(self, callback):
        if callback not in self._callbacks:
            self._callbacks.append(callback)
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_events())

    def remove_push_callback(self, callback):
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    async def _poll_events(self):
        from common.services.reply_state import chat_event
        store = ReplyState(self.sessions)
        try:
            while self._connected and self._callbacks:
                try:
                    async with self.sessions() as session:
                        account = await session.get(XYAccount, self.account_row_id)
                        if (account is None or account.owner_id != self.owner_id
                                or account.account_id != self.account_id or account.status != 'active'):
                            self._connected = False
                            return
                    rows = await store.events(self.account_id, after=self._event_cursor)
                    for row in rows:
                        for callback in tuple(self._callbacks):
                            try:
                                await callback(chat_event(row))
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                # A disconnected subscriber must not block other subscribers.
                                # The frontend recovers missed records by its durable cursor.
                                continue
                        self._event_cursor = row['cursor']
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # Read-only event poll; no platform operation is retried.
                await asyncio.sleep(1)
        finally:
            if self._poll_task is asyncio.current_task():
                self._poll_task = None
