"""跨进程回复状态：唯一事件、连续历史、人工暂停、出站幂等和只回一次预占。"""
from __future__ import annotations
import asyncio
import hashlib
import json
import math
import time
from sqlalchemy import select, insert, update, delete, and_, or_
from sqlalchemy.exc import IntegrityError
from common.models.reply_state import (
    metadata, TABLES, reply_events, reply_outbox, reply_once_slots, reply_pauses,
    reply_policies, exclusive_replies, advanced_filters, reply_stream_heads,
)


def identity(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def normalize_chat(chat_id: str) -> str:
    return str(chat_id).removesuffix('@goofish')


class MessageRejectedError(Exception):
    """平台明确返回未受理消息；与网络超时区分。"""


class ReplyState:
    def __init__(self, sessions=None):
        if sessions is None:
            from common.db.session import async_session_maker
            sessions = async_session_maker
        self.sessions = sessions

    async def _stream_lock(self, session, account_id):
        """DB行锁从分配cursor之前持续到提交；避免后提交小cursor被补拉跳过。"""
        dialect = session.bind.dialect.name
        if dialect == 'mysql':
            from sqlalchemy.dialects.mysql import insert as upsert
            stmt = upsert(reply_stream_heads).values(account_id=account_id, version=0)
            stmt = stmt.on_duplicate_key_update(version=reply_stream_heads.c.version + 1)
        else:
            from sqlalchemy.dialects.sqlite import insert as upsert
            stmt = upsert(reply_stream_heads).values(account_id=account_id, version=0)
            stmt = stmt.on_conflict_do_update(index_elements=['account_id'],
                set_={'version': reply_stream_heads.c.version + 1})
        await session.execute(stmt)

    async def record_message(self, account_id, chat_id, event_id, role, content, sender_id='',
                             origin='platform', item_id='', content_type='text', status='confirmed',
                             occurred_at=None, sender_name='', avatar=''):
        chat_id = normalize_chat(chat_id)
        if not chat_id or not event_id:
            raise ValueError('消息需带会话及事件身份')
        if role == 'assistant' and origin in {'manual', 'platform'}:
            async with self.sessions() as session:
                linked = (await session.execute(select(reply_outbox).where(
                    reply_outbox.c.account_id == account_id, reply_outbox.c.chat_id == chat_id,
                    reply_outbox.c.result['messageId'].as_string() == str(event_id)))).mappings().first()
            if linked:
                await self.reconcile(account_id, chat_id, linked['request_id'], dict(source='platform_history',
                    account_id=account_id, chat_id=chat_id, messageId=str(event_id), status='confirmed'))
                async with self.sessions() as session:
                    row = (await session.execute(select(reply_events).where(reply_events.c.account_id == account_id,
                        reply_events.c.chat_id == chat_id, reply_events.c.event_id == 'out:' + linked['request_id']))).mappings().one()
                return dict(row), False
        where = and_(reply_events.c.account_id == account_id, reply_events.c.chat_id == chat_id,
                     reply_events.c.event_id == str(event_id))
        async with self.sessions() as session:
            try:
                await self._stream_lock(session, account_id)
                await session.execute(insert(reply_events).values(
                    account_id=account_id, chat_id=chat_id, event_id=str(event_id), message_id=str(event_id), role=role,
                    content=content, sender_id=sender_id, origin=origin, item_id=item_id or '',
                    content_type=content_type, status=status, occurred_at=occurred_at or time.time(),
                    sender_name=sender_name, avatar=avatar, version=1))
                await session.commit()
                fresh = True
            except IntegrityError:
                await session.rollback()
                fresh = False
            row = (await session.execute(select(reply_events).where(where))).mappings().one()
            return dict(row), fresh

    async def history(self, account_id, chat_id, limit=100, before=None):
        scope = (reply_events.c.account_id == account_id, reply_events.c.chat_id == normalize_chat(chat_id))
        query = select(reply_events).where(*scope, reply_events.c.status != 'failed', reply_events.c.origin != 'receipt')
        async with self.sessions() as session:
            if before is not None:
                anchor = (await session.execute(select(reply_events.c.occurred_at).where(*scope,
                    reply_events.c.cursor == before))).scalar_one_or_none()
                if anchor is None:
                    return []
                query = query.where(or_(reply_events.c.occurred_at < anchor,
                    and_(reply_events.c.occurred_at == anchor, reply_events.c.cursor < before)))
            rows = (await session.execute(query.order_by(reply_events.c.occurred_at.desc(), reply_events.c.cursor.desc())
                .limit(max(1, min(limit, 200))))).mappings().all()
            return [dict(r) for r in reversed(rows)]

    async def events(self, account_id, after=0, limit=100):
        async with self.sessions() as session:
            rows = (await session.execute(select(reply_events).where(
                reply_events.c.account_id == account_id, reply_events.c.cursor > after)
                .order_by(reply_events.c.cursor).limit(max(1, min(limit, 200))))).mappings().all()
            return [dict(r) for r in rows]

    async def reserve_once(self, account_id, chat_id, scope, request_id):
        chat_id = normalize_chat(chat_id)
        async with self.sessions() as session:
            try:
                await session.execute(insert(reply_once_slots).values(id=identity(account_id, chat_id, scope or ''),
                    account_id=account_id, chat_id=chat_id, scope=scope or '', request_id=request_id, status='reserved'))
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def settle_once(self, account_id, chat_id, scope, request_id, status):
        if status not in {'failed', 'unknown', 'confirmed'}:
            raise ValueError('未知发送阶段')
        where = and_(reply_once_slots.c.id == identity(account_id, normalize_chat(chat_id), scope or ''),
                     reply_once_slots.c.request_id == request_id, reply_once_slots.c.status != 'confirmed')
        stmt = delete(reply_once_slots).where(where) if status == 'failed' else update(reply_once_slots).where(where).values(status=status)
        async with self.sessions() as session:
            await session.execute(stmt)
            await session.commit()

    async def pause(self, account_id, chat_id, minutes, now=None, *, extend_only=False):
        if minutes is None:
            minutes = 10
        if minutes < 0:
            raise ValueError('暂停时长应大于等于0')
        chat_id = normalize_chat(chat_id)
        key = identity(account_id, chat_id)
        until = (time.time() if now is None else now) + minutes * 60
        async with self.sessions() as session:
            try:
                await session.execute(insert(reply_pauses).values(id=key, account_id=account_id, chat_id=chat_id, until=until, version=1))
                await session.commit()
            except IntegrityError:
                await session.rollback()
                from sqlalchemy import case
                value=case((reply_pauses.c.until > until,reply_pauses.c.until),else_=until) if extend_only else until
                await session.execute(update(reply_pauses).where(reply_pauses.c.id == key).values(until=value, version=reply_pauses.c.version + 1))
                await session.commit()

    async def pause_remaining(self, account_id, chat_id, now=None):
        async with self.sessions() as session:
            until = (await session.execute(select(reply_pauses.c.until).where(reply_pauses.c.id == identity(account_id, normalize_chat(chat_id))))).scalar_one_or_none()
            return max(0, math.ceil((until or 0) - (time.time() if now is None else now)))

    async def send(self, account_id, chat_id, request_id, content, transport, *,
                   pause_minutes=None, origin='manual', content_type='text', sender_id='', fingerprint=None, item_id='', correlation=None):
        """预写出站任务后才调用发送；回执未知保留，不凭超时重发。"""
        chat_id = normalize_chat(chat_id)
        key, digest = identity(account_id, chat_id, request_id), identity(content, content_type, fingerprint, origin, item_id)
        async with self.sessions() as session:
            try:
                await self._stream_lock(session, account_id)
                await session.execute(insert(reply_outbox).values(id=key, account_id=account_id, chat_id=chat_id,
                    request_id=request_id, content_hash=digest, status='submitted',
                    result={'correlation': correlation or {}, 'origin': origin}, version=1))
                await session.execute(insert(reply_events).values(account_id=account_id, chat_id=chat_id,
                    event_id='out:' + request_id, message_id='out:' + request_id, role='assistant',
                    content=content, sender_id=sender_id, origin=origin, item_id=item_id or '',
                    content_type=content_type, status='submitted', occurred_at=time.time(), version=1))
                if origin == 'manual' and pause_minutes is not None:
                    if pause_minutes < 0:
                        raise ValueError('暂停时长应大于等于0')
                    values = dict(id=identity(account_id, chat_id), account_id=account_id, chat_id=chat_id,
                        until=time.time() + pause_minutes * 60, version=1)
                    if session.bind.dialect.name == 'mysql':
                        from sqlalchemy.dialects.mysql import insert as upsert
                        stmt = upsert(reply_pauses).values(**values).on_duplicate_key_update(
                            until=values['until'], version=reply_pauses.c.version + 1)
                    else:
                        from sqlalchemy.dialects.sqlite import insert as upsert
                        stmt = upsert(reply_pauses).values(**values).on_conflict_do_update(index_elements=['id'],
                            set_={'until': values['until'], 'version': reply_pauses.c.version + 1})
                    await session.execute(stmt)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                prior = (await session.execute(select(reply_outbox).where(reply_outbox.c.id == key))).mappings().first()
                if not prior:
                    raise ValueError('发送历史身份冲突')
                if prior['content_hash'] != digest:
                    raise ValueError('同一请求身份对应了不同消息')
                return {'requestId': request_id, 'status': prior['status'], 'version': prior['version'], **prior['result']}
        try:
            result = await transport()
            result = result if isinstance(result, dict) else {}
            status = result.get('status')
            if status not in {'confirmed', 'failed', 'unknown'}:
                # 仅明确业务回执算确认；messageId/成功提交本身不是确认。
                status = 'confirmed' if result.get('confirmed') is True else 'unknown'
        except MessageRejectedError as exc:
            result, status = {'error': str(exc)[:300]}, 'failed'
        except (Exception, asyncio.CancelledError) as exc:
            result, status = {'error': type(exc).__name__}, 'unknown'
        result = {k: v for k, v in result.items() if k in {'messageId', 'imageUrl', 'error', 'protocol_mid', 'uuid'}}
        return await self.settle_outbound(account_id, chat_id, request_id, status, result)

    async def dispatch_message(self, request, transport, *, pause_minutes=None, sender_id=''):
        """已有账号执行方的发送适配；不申请租约、不另建IM、不重试传输。

        transport执行现有RPC/WS命令，返回OperationResult；调用方保留该结果，
        此返回值用于聊天状态。应放在执行方分配mid之前的业务入口。
        """
        if request.command not in {'send_text_message', 'send_image_message'}:
            raise ValueError('仅适配聊天出站命令')
        payload = request.payload
        kind = 'image' if request.command == 'send_image_message' else 'text'
        origin = payload.get('origin', 'program')
        async def submit():
            operation = await transport()
            result = operation.result or {}
            # dispatcher超时后仍保留note_submission中的mid/uuid，绝不把mid冒充messageId。
            return {'status': operation.status, 'messageId': result.get('messageId', ''),
                'protocol_mid': result.get('mid', ''), 'uuid': result.get('uuid', ''),
                **({'error': operation.error_code} if operation.error_code else {})}
        return await self.send(request.account_id, payload['cid'], request.request_id,
            payload['image_url'] if kind == 'image' else payload['text'], submit,
            origin=origin, content_type=kind, sender_id=sender_id, pause_minutes=pause_minutes,
            item_id=payload.get('item_id', ''), fingerprint=payload['to_user_id'],
            correlation={'dispatch_request_id': request.request_id, 'owner_id': request.owner_id,
                'generation': request.generation, 'credential_version': request.credential_version,
                'config_version': request.config_version})

    async def outbound(self, account_id, chat_id, request_id):
        async with self.sessions() as session:
            row = (await session.execute(select(reply_outbox).where(reply_outbox.c.id ==
                identity(account_id, normalize_chat(chat_id), request_id)))).mappings().first()
        if not row:
            raise ValueError('发送意图不存在')
        return dict(row)

    async def settle_outbound(self, account_id, chat_id, request_id, status, result=None):
        if status not in {'confirmed', 'failed', 'unknown'}:
            raise ValueError('未知发送状态')
        key = identity(account_id, normalize_chat(chat_id), request_id)
        async with self.sessions() as session:
            row = (await session.execute(select(reply_outbox).where(reply_outbox.c.id == key)
                .with_for_update())).mappings().first()
            if not row:
                raise ValueError('发送意图不存在')
            details = dict(row['result'])
            if row['status'] not in {'confirmed', 'failed'}:
                details.update(result or {})
                await session.execute(update(reply_outbox).where(reply_outbox.c.id == key)
                    .values(status=status, result=details, version=row['version'] + 1))
                await session.commit()
            else:
                status = row['status']
        # 重复核实也修复进程在outbox提交后、历史提交前退出的状态。
        await self.update_message_status(account_id, chat_id, 'out:' + request_id, status)
        await self._settle_correlated_once(account_id, chat_id, details)
        return {'requestId': request_id, 'status': status, 'version': row['version'] + (row['status'] not in {'confirmed', 'failed'}), **details}

    async def _settle_correlated_once(self, account_id, chat_id, details):
        once = details.get('correlation', {}).get('once')
        if not once or len(once) != 3:
            return
        async with self.sessions() as session:
            rows = (await session.execute(select(reply_outbox).where(reply_outbox.c.account_id == account_id,
                reply_outbox.c.chat_id == normalize_chat(chat_id)))).mappings().all()
        related = [r for r in rows if r['result'].get('correlation', {}).get('once') == once]
        # 一段已确认即表明确实回复过，名额不再释放；全部明确未发才可释放。
        statuses = {r['status'] for r in related}
        status = 'confirmed' if 'confirmed' in statuses else 'failed' if statuses == {'failed'} else 'unknown'
        await self.settle_once(account_id, *once, status)

    async def reconcile(self, account_id, chat_id, request_id, evidence):
        """仅供可信平台查询结果调用，HTTP入口不接受用户自报发送状态。"""
        row = await self.outbound(account_id, chat_id, request_id)
        mid = row['result'].get('messageId')
        if (evidence.get('source') not in {'platform_history', 'platform_receipt'}
                or evidence.get('account_id') != account_id
                or normalize_chat(evidence.get('chat_id', '')) != normalize_chat(chat_id)
                or not mid or evidence.get('messageId') != mid
                or evidence.get('status') not in {'confirmed', 'failed'}
                or (evidence.get('status') == 'failed' and evidence.get('source') != 'platform_receipt')):
            raise ValueError('核实证据未匹配账号、会话和平台消息身份')
        return await self.settle_outbound(account_id, chat_id, request_id, evidence['status'],
            {'evidence': {k: evidence[k] for k in ('source', 'messageId', 'status')}, 'verified_at': time.time()})

    async def update_message_status(self, account_id, chat_id, event_id, status):
        if status not in {'submitted', 'unknown', 'failed', 'confirmed'}:
            raise ValueError('未知发送状态')
        async with self.sessions() as session:
            await self._stream_lock(session, account_id)
            where = and_(reply_events.c.account_id == account_id, reply_events.c.chat_id == normalize_chat(chat_id),
                reply_events.c.event_id == event_id)
            row = (await session.execute(select(reply_events).where(where).with_for_update())).mappings().first()
            if not row or row['status'] == status or row['status'] in {'confirmed', 'failed'}:
                return
            values = dict(row)
            values.pop('cursor')
            values.update(event_id='state:' + identity(event_id, row['version'] + 1),
                          version=row['version'] + 1, status=status, origin='receipt')
            await session.execute(update(reply_events).where(where, reply_events.c.version == row['version'])
                .values(status=status, version=values['version']))
            await session.execute(insert(reply_events).values(**values))
            await session.commit()

    async def policy(self, account_id):
        async with self.sessions() as session:
            row = (await session.execute(select(reply_policies).where(reply_policies.c.account_id == account_id))).mappings().first()
            return dict(row) if row else dict(account_id=account_id, strategy='ai_first', version=0, block_personal=True, block_platform=False)

    async def exclusive(self, account_id, item_id):
        if not item_id:
            return None
        async with self.sessions() as session:
            row = (await session.execute(select(exclusive_replies).where(exclusive_replies.c.account_id == account_id,
                exclusive_replies.c.item_id == item_id, exclusive_replies.c.enabled.is_(True)))).mappings().first()
            return dict(row) if row else None

    async def filters(self, account_id, *, owner_id=None, include_disabled=False):
        async with self.sessions() as session:
            # Old account-scoped rules keep working; owner lookup is needed only for shared rules.
            global_rule=await session.scalar(select(advanced_filters.c.id).where(advanced_filters.c.account_id=='').limit(1))
            if global_rule and owner_id is None:
                from common.models.xy_account import XYAccount
                owner_id=await session.scalar(select(XYAccount.owner_id).where(XYAccount.account_id==account_id))
            scope=advanced_filters.c.account_id==account_id
            if owner_id is not None:
                scope=or_(scope,and_(advanced_filters.c.account_id=='',advanced_filters.c.owner_id==owner_id))
            query=select(advanced_filters).where(scope)
            if not include_disabled: query=query.where(advanced_filters.c.enabled.is_(True))
            rows=(await session.execute(query.order_by(advanced_filters.c.id))).mappings().all()
            return [dict(row) for row in rows]

    async def filter_decision(self, account_id, chat_id, content, source, item_id=''):
        from common.services.reply_policy import evaluate_filters
        decision=evaluate_filters(await self.filters(account_id),content,source,item_id)
        if 'pause' in decision.actions:
            minutes=decision.pause_minutes
            if decision.inherit_pause:
                from common.models.xy_account import XYAccount
                async with self.sessions() as session:
                    configured=await session.scalar(select(XYAccount.pause_duration).where(XYAccount.account_id==account_id))
                minutes=max(minutes,configured if configured is not None else 10)
            if minutes>0:
                await self.pause(account_id,chat_id,minutes,extend_only=True)
        return decision


    async def reply_blocked(self, owner_id, account_id, buyer_id, item_id=''):
        """只决定回复，不更改履约状态；个人/平台名单开关分别生效。"""
        from sqlalchemy import or_
        from common.models.xy_personal_blacklist import XYPersonalBlacklist
        from common.models.xy_platform_blacklist import XYPlatformBlacklist
        policy = await self.policy(account_id)
        async with self.sessions() as session:
            if policy['block_personal']:
                personal = XYPersonalBlacklist.__table__
                found = (await session.execute(select(personal.c.id).where(
                    personal.c.owner_id == owner_id, personal.c.buyer_id == buyer_id, personal.c.is_enabled.is_(True),
                    or_(personal.c.account_id.is_(None), personal.c.account_id == '', personal.c.account_id == account_id),
                    or_(personal.c.item_id.is_(None), personal.c.item_id == '', personal.c.item_id == (item_id or ''))).limit(1))).first()
                if found:
                    return True
            if policy['block_platform']:
                platform = XYPlatformBlacklist.__table__
                found = (await session.execute(select(platform.c.id).where(platform.c.owner_id == owner_id,
                    platform.c.buyer_id == buyer_id).limit(1))).first()
                if found:
                    return True
        return False


def platform_send_result(result):
    """将IM业务回执映射为发送阶段，空响应/仅messageId不等于确认。"""
    response = result.get('response') or {}
    body = response.get('body') or {}
    if isinstance(body, dict) and body.get('reason'):
        status = 'failed'
    elif response.get('code') == 200:
        status = 'confirmed'
    else:
        status = 'unknown'
    return {'status': status, 'messageId': result.get('messageId', ''), **({'error': body['reason']} if status == 'failed' else {})}


def chat_event(row):
    return {'event': 'new_message', 'event_id': row['event_id'], 'version': row['version'], 'cursor': row['cursor'],
        'cid': row['chat_id'], 'account_id': row['account_id'], 'message': {
            'messageId': row['message_id'], 'senderId': row['sender_id'], 'senderName': row['sender_name'],
            'isSelf': row['role'] == 'assistant', 'text': row['content'] if row['content_type'] == 'text' else '',
            'images': [row['content']] if row['content_type'] == 'image' else [], 'type': row['content_type'],
            'time': int(row['occurred_at'] * 1000), 'status': row['status'], 'version': row['version']}}
