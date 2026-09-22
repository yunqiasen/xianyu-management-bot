"""跨服务固定命令契约、持久操作账本和外发上下文。此模块从不申请账号租约。"""
from __future__ import annotations

from contextvars import ContextVar
from hashlib import sha256
import json
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from common.models.account_operation import AccountOperation
from common.models.xy_account import XYAccount
from common.services.account_policy import snapshot
from common.services.account_request_budget import RequestBudgetPolicy, account_risk_config


Command = Literal['get_conversations', 'get_messages', 'send_text_message', 'send_image_message',
                  'sync_items', 'publish_single', 'polish_item', 'rate_buyer', 'request_red_flower', 'monitor_search_page', 'platform_request', 'recall_message', 'upload_image', 'upload_video']
COMMANDS = frozenset(Command.__args__)
ID = Annotated[str, Field(min_length=1, max_length=96, pattern=r'^[a-zA-Z0-9_.:@-]+$')]


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Conversations(Payload):
    start_timestamp: int | None = Field(default=None, ge=0, le=9007199254740991)
    limit: int = Field(default=20, ge=1, le=100)


class Messages(Conversations):
    cid: ID


class OutboundMessage(Payload):
    origin: Literal['manual', 'program', 'assistant', 'ai'] = 'manual'
    item_id: str = Field(default='', max_length=64)


class TextMessage(OutboundMessage):
    cid: ID
    to_user_id: ID
    text: str = Field(min_length=1, max_length=10000)


class ImageMessage(OutboundMessage):
    cid: ID
    to_user_id: ID
    image_url: str = Field(max_length=2048)
    width: int = Field(default=800, ge=1, le=20000)
    height: int = Field(default=600, ge=1, le=20000)

    @field_validator('image_url')
    @classmethod
    def cdn_only(cls, value):
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not (host == 'alicdn.com' or host.endswith('.alicdn.com'))):
            raise ValueError('图片需使用已上传的闲鱼 CDN 地址')
        return value


class RecallMessage(Payload):
    message_id: ID


class SyncItems(Payload):
    pass


class PublishSingle(Payload):
    item_data: dict


class PolishItem(Payload):
    item_id: ID


class RateBuyer(Payload):
    order_no: ID
    feedback: str = Field(default='不错的买家', min_length=1, max_length=1000)


class RedFlower(Payload):
    order_no: ID


from common.services.listing_monitor_transport import MonitorSearchPage
from common.services.platform_rpc import PlatformRequest
from common.services.image_gateway import ImageUploadRequest
from common.services.video_gateway import VideoUploadRequest, MAX_VIDEO_BYTES

PAYLOADS = dict(upload_video=VideoUploadRequest, upload_image=ImageUploadRequest, recall_message=RecallMessage, platform_request=PlatformRequest, monitor_search_page=MonitorSearchPage, get_conversations=Conversations, get_messages=Messages, send_text_message=TextMessage,
               send_image_message=ImageMessage, sync_items=SyncItems, publish_single=PublishSingle,
               polish_item=PolishItem, rate_buyer=RateBuyer, request_red_flower=RedFlower)


class DispatchRequest(Payload):
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_.:@-]+$')
    owner_id: int = Field(gt=0)
    account_id: str = Field(min_length=1, max_length=80)
    generation: int = Field(ge=0)
    credential_version: int = Field(ge=0)
    config_version: int = Field(ge=0)
    command: Command
    payload: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_payload(self):
        self.payload = PAYLOADS[self.command].model_validate(self.payload).model_dump()
        from common.services.reply_images import MAX_BYTES
        # Image bytes are base64 in the internal RPC; keep the documented 10MB file limit.
        limit = 4 * ((MAX_BYTES + 2) // 3) + 64 if self.command == 'upload_image' else 256 * 1024
        if self.command == 'upload_video':
            limit = 4*((MAX_VIDEO_BYTES+2)//3)+512
        if len(json.dumps(self.payload, ensure_ascii=False).encode()) > limit:
            raise ValueError('请求正文过大')
        return self


class OperationResult(BaseModel):
    request_id: str
    owner_id: int
    account_id: str
    command: str
    status: Literal['submitted', 'confirmed', 'failed', 'unknown']
    result: dict | None = None
    error_code: str | None = None
    retry_after: float | None = None
    generation: int
    credential_version: int
    config_version: int


class ExecutorStatus(BaseModel):
    account_id: str
    owner_id: int
    ready: bool
    reason: str | None = None
    generation: int
    credential_version: int
    config_version: int


class DispatchError(RuntimeError):
    def __init__(self, code, http_status=409):
        super().__init__(code)
        self.code, self.http_status = code, http_status


class PlatformFailure(DispatchError):
    def __init__(self, code, retry_after=None, *, result=None):
        super().__init__(code)
        self.retry_after, self.result = retry_after, result


def operation_key(owner_id, account_id, request_id):
    return sha256(json.dumps([owner_id, account_id, request_id], separators=(',', ':')).encode()).hexdigest()


def fingerprint(request):
    # 内容身份不含执行快照；同一请求在接任后仍只查询旧事实，不重新发送。
    content = request.model_dump(exclude={'generation', 'credential_version', 'config_version'})
    return sha256(json.dumps(content, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode()).hexdigest()


def clean_result(value):
    """协议结果脱敏；异常文本永不落库。保留聊天事实，不存认证信息。"""
    if isinstance(value, dict):
        return {k: clean_result(v) for k, v in value.items()
                if not any(word in k.lower().replace('-', '_') for word in
                           ('cookie', 'token', 'password', 'secret', 'authorization', 'proxy_pass'))}
    if isinstance(value, list): return [clean_result(v) for v in value]
    return value


class OperationStore:
    def __init__(self, sessions): self.sessions = sessions

    async def account(self, owner_id, account_id):
        async with self.sessions() as db:
            account = (await db.execute(select(XYAccount).where(XYAccount.owner_id == owner_id,
                                        XYAccount.account_id == account_id))).scalar_one_or_none()
            if account is None: raise DispatchError('account_not_found', 404)
            return account

    @staticmethod
    def result(row):
        return OperationResult(**{key: getattr(row, key) for key in OperationResult.model_fields})

    async def get(self, owner_id, account_id, request_id):
        await self.account(owner_id, account_id)
        async with self.sessions() as db:
            key = operation_key(owner_id, account_id, request_id)
            row = await db.get(AccountOperation, key)
            if row is None: raise DispatchError('operation_not_found', 404)
            if row.status == 'submitted' and row.deadline < time.time():
                await db.execute(update(AccountOperation).where(AccountOperation.id == key,
                    AccountOperation.status == 'submitted').values(status='unknown', error_code='executor_interrupted'))
                await db.commit()
                await db.refresh(row)
            return self.result(row)

    async def existing(self, request):
        async with self.sessions() as db:
            row = await db.get(AccountOperation, operation_key(request.owner_id, request.account_id, request.request_id))
            if row is None: return None
            if row.fingerprint != fingerprint(request): raise DispatchError('request_conflict')
        return await self.get(request.owner_id, request.account_id, request.request_id)

    async def claim(self, request, deadline):
        row = AccountOperation(id=operation_key(request.owner_id, request.account_id, request.request_id),
            fingerprint=fingerprint(request), status='submitted', deadline=deadline,
            **{key: getattr(request, key) for key in ('request_id', 'owner_id', 'account_id', 'command',
                                                      'generation', 'credential_version', 'config_version')})
        async with self.sessions() as db:
            db.add(row)
            try: await db.commit()
            except IntegrityError:
                await db.rollback()
                existing = await self.existing(request)
                if existing is None: raise DispatchError('operation_store_unavailable', 503)
                return existing
        return None

    async def note_submission(self, request, evidence):
        # 先保存协议关联身份，再外发。超时后仍可按 mid/uuid/资源 ID 核对。
        async with self.sessions() as db:
            changed = await db.execute(update(AccountOperation).where(
                AccountOperation.id == operation_key(request.owner_id, request.account_id, request.request_id),
                AccountOperation.status == 'submitted', AccountOperation.deadline > time.time()
            ).values(result=clean_result(evidence)))
            if changed.rowcount != 1: raise DispatchError('operation_not_submitted')
            await db.commit()

    async def finish(self, request, status, result=None, error_code=None, retry_after=None):
        values = dict(status=status, error_code=error_code, retry_after=retry_after)
        if result is not None: values['result'] = clean_result(result)
        async with self.sessions() as db:
            await db.execute(update(AccountOperation).where(
                AccountOperation.id == operation_key(request.owner_id, request.account_id, request.request_id),
                AccountOperation.status == 'submitted').values(**values))
            await db.commit()
        return await self.get(request.owner_id, request.account_id, request.request_id)


CURRENT_OPERATION: ContextVar['ExecutionContext | None'] = ContextVar('account_operation', default=None)


class ExecutionContext:
    def __init__(self, request, live, store, budget, risk_loader=account_risk_config):
        self.request, self.live, self.store, self.budget = request, live, store, budget
        self.risk_loader = risk_loader
        self.external_started = False
        self.external_count = 0
        self._transport_prepared = False

    async def check(self):
        from common.services.account_execution import ExecutionLost
        request = self.request
        runtime = getattr(self.live, '_account_runtime', None)
        if (runtime is None or runtime.account_id != request.account_id or runtime.owner_id != request.owner_id):
            raise DispatchError('not_account_executor')
        account = await self.store.account(request.owner_id, request.account_id)
        state = snapshot(account)
        if any(state[key] != getattr(request, key) for key in ('generation', 'credential_version', 'config_version')):
            raise DispatchError('stale_account_version')
        if state.get('recovery_running') or state.get('next_retry_at', 0) > time.time():
            raise DispatchError('account_recovering')
        if state['business_state'] != 'ready' or state.get('connection_state') != 'connected':
            raise DispatchError('account_not_ready')
        try:
            await self.live._check_account_execution()
        except ExecutionLost as exc:
            raise DispatchError('execution_lost') from exc
        return account

    async def check_outbound(self):
        if self.request.command not in {'send_text_message', 'send_image_message'}:
            return
        from common.services.reply_state import ReplyState
        from common.services.reply_policy import evaluate_filters
        replies = ReplyState(self.store.sessions)
        payload, request = self.request.payload, self.request
        buyer = payload['to_user_id'].removesuffix('@goofish')
        if await replies.reply_blocked(request.owner_id, request.account_id, buyer, payload['item_id']):
            raise DispatchError('outbound_blacklisted')
        if payload['origin'] != 'manual' and await replies.pause_remaining(request.account_id, payload['cid']):
            raise DispatchError('conversation_paused')
        source = 'ai' if payload['origin'] == 'ai' else 'assistant'
        decision = evaluate_filters(await replies.filters(request.account_id),
            payload.get('text', payload.get('image_url', '')), source, payload['item_id'])
        if decision.blocks_reply or (source == 'ai' and 'skip_ai' in decision.actions):
            raise DispatchError('outbound_filtered')

    async def before_external(self):
        account = await self.check()
        await self.check_outbound()
        policy = RequestBudgetPolicy.from_risk_config(self.risk_loader(account))
        async def validate_waiter():
            await self.check_outbound()
            await self.check()
        await self.budget.acquire(self.request.account_id, policy, check=validate_waiter)
        # 预算 Redis IO 期间可能停用、切代理、改规则或失去租约，实际外发前再查。
        await self.check_outbound()
        await self.check()
        self.external_started = True
        self.external_count += 1
        self._transport_prepared = True

    async def before_transport(self):
        """Consume one reservation at the real transport; never double-charge an RPC."""
        if not self._transport_prepared:
            await self.before_external()
        self._transport_prepared = False
        await self.check_outbound()
        await self.check()


async def before_platform_request(account_id):
    """公共 HTTP / WS / 浏览器每次实际外发前的注入点，缺少执行上下文即停止。"""
    context = CURRENT_OPERATION.get()
    if context is None or context.request.account_id != account_id:
        raise DispatchError('missing_execution_context')
    await context.before_external()


class AccountDispatchClient:
    """web / scheduler 共用的内部 RPC 客户端；不申请租约、不登录、不自动重发。"""
    def __init__(self, base_url, token, *, http=None, timeout=75):
        from common.utils.internal_auth import build_internal_auth_headers
        parsed = urlsplit(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('内部服务地址无效')
        self.base_url, self.headers = base_url.rstrip('/'), build_internal_auth_headers(token)
        self.http, self.timeout = http, timeout

    async def _call(self, method, path, **kwargs):
        import httpx
        kwargs.update(headers=self.headers, timeout=self.timeout)
        if self.http is not None:
            return await self.http.request(method, self.base_url + path, **kwargs)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as http:
            return await http.request(method, self.base_url + path, **kwargs)

    @staticmethod
    def _decode(response):
        if response.status_code >= 400:
            try: code = response.json().get('detail', {}).get('code', 'rpc_rejected')
            except (ValueError, AttributeError): code = 'rpc_rejected'
            raise DispatchError(code, response.status_code)
        return OperationResult.model_validate(response.json())

    async def execute(self, request):
        import httpx
        request = DispatchRequest.model_validate(request.model_dump() if isinstance(request, DispatchRequest) else request)
        try:
            response = await self._call('POST', '/internal/account-operations', json=request.model_dump())
            if response.status_code >= 500:
                # 501 明确未支持，不属于已执行结果丢失。
                if response.status_code == 501: return self._decode(response)
                raise httpx.TransportError('rpc_response_unverified')
            return self._decode(response)
        except (httpx.TransportError, ValueError):
            return OperationResult(**{key: getattr(request, key) for key in
                ('request_id', 'owner_id', 'account_id', 'command', 'generation', 'credential_version', 'config_version')},
                status='unknown', error_code='rpc_result_unverified')

    async def get_executor(self, owner_id, account_id):
        from urllib.parse import quote
        response = await self._call('GET', '/internal/account-operations/executors/' + quote(account_id, safe=''),
                                    params={'owner_id': owner_id})
        if response.status_code >= 400:
            raise DispatchError('executor_status_unavailable', response.status_code)
        status = ExecutorStatus.model_validate(response.json())
        if status.owner_id != owner_id or status.account_id != account_id:
            raise DispatchError('executor_identity_mismatch')
        return status

    async def get_operation(self, owner_id, account_id, request_id):
        from urllib.parse import quote
        return self._decode(await self._call('GET', '/internal/account-operations/' + quote(request_id, safe=''),
                                           params={'owner_id': owner_id, 'account_id': account_id}))

    async def submit(self, *, owner_id, account_id, request_id, command, payload, session):
        """调用方已有用户身份；本地读快照，WS 外发前再次核验。不接受调用方提供 Cookie/代理。"""
        account = (await session.execute(select(XYAccount).where(XYAccount.owner_id == owner_id,
                                    XYAccount.account_id == account_id).execution_options(populate_existing=True))).scalar_one_or_none()
        if account is None: raise DispatchError('account_not_found', 404)
        state = snapshot(account)
        return await self.execute(DispatchRequest(request_id=request_id, owner_id=owner_id, account_id=account_id,
            command=command, payload=payload, **{key: state[key] for key in
                                               ('generation', 'credential_version', 'config_version')}))
