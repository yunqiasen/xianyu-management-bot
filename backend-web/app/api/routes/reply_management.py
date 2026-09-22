"""回复策略、专属规则、实时补拉与高级过滤；挂载于现有聊天路由。"""
from __future__ import annotations
from typing import Literal
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, insert, update, delete
from sqlalchemy.exc import IntegrityError
from app.api.deps import get_current_active_user, get_db_session
from common.models.xy_account import XYAccount
from common.models.user import User
from common.models.reply_state import reply_policies, exclusive_replies, advanced_filters, reply_events
from common.services.reply_state import ReplyState, identity
from common.services.reply_policy import validate_filter
from common.utils.auth_scope import is_admin_user

router = APIRouter(prefix='/reply-controls', tags=['reply-controls'])


def get_reply_state():
    return ReplyState()


async def owned_account(account_id: str, user: User = Depends(get_current_active_user), db=Depends(get_db_session)):
    query = select(XYAccount).where(XYAccount.account_id == account_id)
    if not is_admin_user(user):
        query = query.where(XYAccount.owner_id == user.id)
    account = (await db.execute(query)).scalar_one_or_none()
    if not account:
        raise HTTPException(404, '账号不存在')
    return account


class PolicyPayload(BaseModel):
    strategy: Literal['legacy', 'ai_first']
    version: int = Field(ge=0)
    block_personal: bool = True
    block_platform: bool = False


@router.get('/{account_id}/policy')
async def get_policy(account=Depends(owned_account), store=Depends(get_reply_state)):
    return {'success': True, 'data': await store.policy(account.account_id)}


@router.put('/{account_id}/policy')
async def save_policy(payload: PolicyPayload, account=Depends(owned_account), store=Depends(get_reply_state)):
    values = payload.model_dump(exclude={'version'})
    async with store.sessions() as session:
        try:
            if payload.version == 0:
                await session.execute(insert(reply_policies).values(account_id=account.account_id, version=1, **values))
            else:
                result = await session.execute(update(reply_policies).where(reply_policies.c.account_id == account.account_id,
                    reply_policies.c.version == payload.version).values(version=payload.version + 1, **values))
                if not result.rowcount:
                    raise HTTPException(409, '配置已更新，请刷新后保存')
            await session.commit()
        except IntegrityError as exc:
            raise HTTPException(409, '配置已更新，请刷新后保存') from exc
    return {'success': True, 'data': await store.policy(account.account_id)}


class ExclusivePayload(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    content: str = Field(default='', max_length=20000)
    image_url: str = Field(default='', max_length=1024)
    enabled: bool = True
    version: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def validate_item(self):
        self.item_id = self.item_id.strip()
        if not self.item_id:
            raise ValueError('商品ID为空')
        if self.image_url and not self.image_url.startswith(('/static/uploads/', 'https://')):
            raise ValueError('图片地址应为上传资源或HTTPS地址')
        return self


class ImportPayload(BaseModel):
    rows: list[dict] = Field(max_length=1000)


async def _save_exclusive(payload, account_id, store, owner_id=None):
    values = payload.model_dump(exclude={'version'})
    key = identity(account_id, payload.item_id)
    async with store.sessions() as session:
        try:
            if payload.version == 0:
                await session.execute(insert(exclusive_replies).values(id=key, account_id=account_id, version=1, **values))
            else:
                result = await session.execute(update(exclusive_replies).where(exclusive_replies.c.id == key,
                    exclusive_replies.c.account_id == account_id, exclusive_replies.c.version == payload.version)
                    .values(version=payload.version + 1, **values))
                if not result.rowcount:
                    raise ValueError('规则已更新，请重新导出或刷新')
            image_id = None
            if payload.image_url.startswith('/static/uploads/replies/'):
                image_id = payload.image_url.rsplit('/', 1)[-1].split('.')[0]
                image = await ReplyImages(store.sessions)._owned(session, owner_id, image_id, lock=True)
                if image['url'] != payload.image_url:
                    raise ValueError('图片资源地址与归属不一致')
            await session.execute(delete(reply_image_refs).where(reply_image_refs.c.source == 'exclusive',
                reply_image_refs.c.source_id == key, reply_image_refs.c.owner_id == owner_id))
            if image_id:
                await session.execute(insert(reply_image_refs).values(id=identity(image_id, 'exclusive', key),
                    image_id=image_id, owner_id=owner_id, source='exclusive', source_id=key))
            await session.commit()
        except IntegrityError as exc:
            raise ValueError('规则已存在，请带上当前版本更新') from exc
    return key


@router.get('/{account_id}/exclusive')
async def list_exclusive(account=Depends(owned_account), store=Depends(get_reply_state)):
    async with store.sessions() as session:
        rows = (await session.execute(select(exclusive_replies).where(exclusive_replies.c.account_id == account.account_id)
            .order_by(exclusive_replies.c.item_id))).mappings().all()
        return {'success': True, 'data': [dict(row) for row in rows]}


@router.post('/{account_id}/exclusive')
async def save_exclusive(payload: ExclusivePayload, account=Depends(owned_account), store=Depends(get_reply_state)):
    try:
        key = await _save_exclusive(payload, account.account_id, store, account.owner_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'success': True, 'data': {'id': key}}


@router.post('/{account_id}/exclusive/import')
async def import_exclusive(payload: ImportPayload, account=Depends(owned_account), store=Depends(get_reply_state)):
    saved, errors = 0, []
    for index, raw in enumerate(payload.rows, 1):
        try:
            await _save_exclusive(ExclusivePayload.model_validate(raw), account.account_id, store, account.owner_id)
            saved += 1
        except ValueError:
            errors.append({'row': index, 'message': '规则无效或版本冲突'})
    return {'success': not errors, 'data': {'saved': saved, 'errors': errors}}


@router.delete('/{account_id}/exclusive/{rule_id}')
async def delete_exclusive(rule_id: str, version: int = Query(ge=1), account=Depends(owned_account), store=Depends(get_reply_state)):
    async with store.sessions() as session:
        result = await session.execute(delete(exclusive_replies).where(exclusive_replies.c.id == rule_id,
            exclusive_replies.c.account_id == account.account_id, exclusive_replies.c.version == version))
        if not result.rowcount:
            raise HTTPException(409, '规则不存在或版本已变化')
        await session.execute(delete(reply_image_refs).where(reply_image_refs.c.source == 'exclusive', reply_image_refs.c.source_id == rule_id, reply_image_refs.c.owner_id == account.owner_id))
        await session.commit()
    return {'success': True}


class FilterPayload(BaseModel):
    all_accounts: bool = Field(default=False, strict=True)
    pause_minutes: int | None = Field(default=None, ge=0, le=1440, strict=True)
    pattern: str = Field(min_length=1, max_length=256)
    match_mode: Literal['contains', 'exact', 'regex'] = 'contains'
    source: Literal['user', 'system', 'ai', 'all'] = 'user'
    item_id: str = Field(default='', max_length=64)
    actions: list[Literal['notify', 'skip_ai', 'skip_reply', 'pause', 'skip_notify']]
    enabled: bool = True
    version: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def bounded_pattern(self):
        validate_filter(self.model_dump())
        return self


def filter_scope(account):
    from sqlalchemy import or_, and_
    return or_(advanced_filters.c.account_id==account.account_id,
               and_(advanced_filters.c.account_id=='',advanced_filters.c.owner_id==account.owner_id))


def filter_values(payload, account):
    return dict(account_id='' if payload.all_accounts else account.account_id,owner_id=account.owner_id,
                **payload.model_dump(exclude={'version','all_accounts'}))


@router.get('/{account_id}/filters')
async def list_filters(account=Depends(owned_account), store=Depends(get_reply_state)):
    rows=await store.filters(account.account_id,owner_id=account.owner_id,include_disabled=True)
    return {'success': True, 'data': [{**row,'all_accounts':row['account_id']==''} for row in rows]}


@router.post('/{account_id}/filters')
async def add_filter(payload: FilterPayload, account=Depends(owned_account), store=Depends(get_reply_state)):
    rule_id = uuid.uuid4().hex
    async with store.sessions() as session:
        await session.execute(insert(advanced_filters).values(id=rule_id,version=1,**filter_values(payload,account)))
        await session.commit()
    return {'success': True, 'data': {'id': rule_id, 'version': 1}}


@router.put('/{account_id}/filters/{rule_id}')
async def update_filter(rule_id: str, payload: FilterPayload, account=Depends(owned_account), store=Depends(get_reply_state)):
    async with store.sessions() as session:
        result = await session.execute(update(advanced_filters).where(advanced_filters.c.id == rule_id,
            filter_scope(account), advanced_filters.c.version == payload.version)
            .values(version=payload.version + 1, **filter_values(payload,account)))
        if not result.rowcount:
            raise HTTPException(409, '规则不存在或版本已变化')
        await session.commit()
    return {'success': True}


@router.delete('/{account_id}/filters/{rule_id}')
async def delete_filter(rule_id: str, version: int = Query(ge=1), account=Depends(owned_account), store=Depends(get_reply_state)):
    async with store.sessions() as session:
        result = await session.execute(delete(advanced_filters).where(advanced_filters.c.id == rule_id,
            filter_scope(account), advanced_filters.c.version == version))
        if not result.rowcount:
            raise HTTPException(409, '规则不存在或版本已变化')
        await session.commit()
    return {'success': True}


from common.services.reply_state import chat_event


@router.get('/{account_id}/events')
async def replay_events(after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=200),
                        account=Depends(owned_account), store=Depends(get_reply_state)):
    rows = await store.events(account.account_id, after, limit)
    return {'success': True, 'data': {'events': [chat_event(r) for r in rows],
        'nextCursor': rows[-1]['cursor'] if rows else after, 'hasMore': len(rows) == limit}}


@router.get('/{account_id}/history/{chat_id}')
async def get_history(chat_id: str, before: int | None = Query(default=None, ge=1), limit: int = Query(default=50, ge=1, le=200),
                      account=Depends(owned_account), store=Depends(get_reply_state)):
    rows = await store.history(account.account_id, chat_id, limit, before)
    return {'success': True, 'data': {'messages': [chat_event(r)['message'] for r in rows],
        'hasMore': len(rows) == limit, 'nextCursor': rows[0]['cursor'] if rows else None,
        'pauseRemaining': await store.pause_remaining(account.account_id, chat_id)}}


from fastapi import File, UploadFile, Response
from common.models.reply_state import reply_images, reply_image_refs
from common.services.reply_images import ReplyImages, MAX_BYTES


@router.get('/{account_id}/images')
async def list_images(account=Depends(owned_account), store=Depends(get_reply_state)):
    async with store.sessions() as session:
        rows = (await session.execute(select(reply_images).where(reply_images.c.owner_id == account.owner_id))).mappings().all()
    return {'success': True, 'data': [{k: v for k, v in row.items() if k != 'path'} for row in rows]}


@router.post('/{account_id}/images')
async def upload_image(image: UploadFile = File(...), account=Depends(owned_account), store=Depends(get_reply_state)):
    try:
        data = await image.read(MAX_BYTES + 1)
        row = await ReplyImages(store.sessions).upload(account.owner_id, data)
        return {'success': True, 'data': {k: v for k, v in row.items() if k != 'path'}}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/{account_id}/images/{image_id}/references')
async def image_references(image_id: str, account=Depends(owned_account), store=Depends(get_reply_state)):
    try:
        return {'success': True, 'data': await ReplyImages(store.sessions).references(account.owner_id, image_id)}
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete('/{account_id}/images/{image_id}')
async def delete_image(image_id: str, account=Depends(owned_account), store=Depends(get_reply_state)):
    try:
        await ReplyImages(store.sessions).delete(account.owner_id, image_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'success': True}

class VerifyOutboundPayload(BaseModel):
    # 禁止客户端上报confirmed；只允许请求服务端查询平台事实。
    model_config = {'extra': 'forbid'}


def get_platform_history_evidence():
    async def fetch(account_id, chat_id):
        from app.services.chat_new.im_session_manager import get_im_session_manager
        from app.api.routes.chat_new import _parse_message
        client = get_im_session_manager().clients.get(account_id)
        if not client or not client.is_connected:
            raise ValueError('账号执行方未连接，保留待核实')
        body = await client.get_messages(cid=chat_id, start_timestamp=None, limit=100)
        if not isinstance(body, dict) or body.get('reason') or not isinstance(body.get('userMessageModels'), list):
            raise ValueError('平台历史查询未取得有效证据')
        return [m for raw in body['userMessageModels'] if (m := _parse_message(raw, client.myid))]
    return fetch


@router.get('/{account_id}/outbound/{chat_id}/{request_id}')
async def get_outbound(chat_id: str, request_id: str, account=Depends(owned_account), store=Depends(get_reply_state)):
    try:
        row = await store.outbound(account.account_id, chat_id, request_id)
        return {'success': True, 'data': {'requestId': request_id, 'status': row['status'], 'version': row['version'], **row['result']}}
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/{account_id}/outbound/{chat_id}/{request_id}/verify')
async def verify_outbound(chat_id: str, request_id: str, payload: VerifyOutboundPayload,
                          account=Depends(owned_account), store=Depends(get_reply_state),
                          fetch=Depends(get_platform_history_evidence)):
    try:
        row = await store.outbound(account.account_id, chat_id, request_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    data = {'requestId': request_id, 'status': row['status'], 'version': row['version'], **row['result']}
    if row['status'] in {'confirmed', 'failed'}:
        # 补齐在出站提交与历史提交之间中断的写回。
        data = await store.settle_outbound(account.account_id, chat_id, request_id, row['status'])
        return {'success': True, 'data': data}
    mid = row['result'].get('messageId')
    if not mid:
        return {'success': True, 'data': {**data, 'verification': '缺少平台消息身份，保留待核实；未重发'}}
    try:
        messages = await fetch(account.account_id, chat_id)
    except Exception:
        return {'success': True, 'data': {**data, 'verification': '平台查询暂未取得证据，保留待核实；未重发'}}
    for message in messages:
        if message.get('messageId') == mid and message.get('isSelf') is True:
            data = await store.reconcile(account.account_id, chat_id, request_id,
                dict(source='platform_history', account_id=account.account_id, chat_id=chat_id,
                     messageId=mid, status='confirmed'))
            return {'success': True, 'data': data}
    return {'success': True, 'data': {**data, 'verification': '当前历史页未发现对应消息，不据此判失败；未重发'}}

class PlatformSyncPayload(BaseModel):
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


def get_official_blacklist_evidence():
    async def fetch(account, chat_id):
        from app.services.chat_new.official_blacklist_service import official_blacklist_request, parse_blacklist_status
        return parse_blacklist_status(await official_blacklist_request(account.cookie, chat_id, 'query', account=account))
    return fetch


@router.post('/{account_id}/platform-blacklist/sync')
async def sync_platform_blacklist(payload: PlatformSyncPayload, account=Depends(owned_account),
                                 store=Depends(get_reply_state), fetch=Depends(get_official_blacklist_evidence)):
    """按已知会话逐个查询。缺证据不删旧名单，分页游标保留失败位置。"""
    from sqlalchemy import func
    from app.services.blacklist_service import BlacklistService
    async with store.sessions() as session:
        query = select(reply_events.c.chat_id, reply_events.c.sender_id,
            func.max(reply_events.c.cursor).label('cursor')).where(reply_events.c.account_id == account.account_id,
                reply_events.c.role == 'user', reply_events.c.sender_id != '').group_by(
                    reply_events.c.chat_id, reply_events.c.sender_id)
        query = query.having(func.max(reply_events.c.cursor) > payload.after).order_by(func.max(reply_events.c.cursor)).limit(payload.limit)
        candidates = (await session.execute(query)).mappings().all()
    synced, cursor, errors = 0, payload.after, []
    for row in candidates:
        try:
            blocked = await fetch(account, row['chat_id'])
            if not isinstance(blocked, bool):
                raise ValueError('缺少明确名单状态')
            async with store.sessions() as session:
                await BlacklistService(session).sync_platform_member(account.owner_id, row['sender_id'], blocked)
            synced += 1
            cursor = row['cursor']
        except Exception:
            errors.append({'chat_id': row['chat_id'], 'message': '平台状态未核实，保留原名单'})
            break
    return {'success': not errors, 'data': {'synced': synced, 'nextCursor': cursor,
        'hasMore': bool(errors) or len(candidates) == payload.limit, 'errors': errors,
        'scope': 'known_conversations'}}
