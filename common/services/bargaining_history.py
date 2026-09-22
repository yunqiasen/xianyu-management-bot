"""DEV45 候选：消费 P2 confirmed 历史；不创建第二套消息事实或协议客户端。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from common.models.bargaining_event import BargainingEvent
from common.services.reply_state import identity, normalize_chat


@dataclass(frozen=True)
class Intent:
    intent: str
    bargaining: bool = False


# 不把普通询价、刀具名词、单独金额或评价当成优惠请求。
_BARGAIN = re.compile(
    r'便宜(?:点|一点|些|一点点|\d)|(?:能|可以|可否|能否|还能|再).{0,4}(?:便宜|优惠|降价|少点|少一点)'
    r'|(?:给|来)(?:个|点|些)优惠|(?:优惠|便宜)(?:点|些|吗|么|不)'
    r'|(?:最低|底价)(?:多少|几|是)|(?:什么|多少)(?:底价)|最低价|实诚价'
    r'|(?:能|再|可以)少(?:点|些|一点|\d)|(?:再|能|可以)降(?:点|些|\d)'
    r'|(?:能|可以|可否)刀|小刀(?:一下|点|吗|不)?|砍价|包个邮'
    r'|\d+(?:\.\d+)?\s*(?:元|块)?\s*(?:可以吗|行吗|卖吗|出吗|卖不卖|出不出|能出|能卖|包邮收|包邮出|我收|我拿|拿下)'
)
_NEGATED = re.compile(r'(?:不|别|不要|不用|无需|不必)(?:再)?(?:砍价|小刀|刀|降价|便宜|优惠)')
_TECH = re.compile(r'怎么用|如何用|参数|规格|型号|坏了|故障|设置|说明书|功能|用法|教程|驱动|还能用|技术|材质|刀片|刀头|转速|充电')
_PRICE = re.compile(r'价格|多少钱|多少元|多少块|原价|售价|报价|含邮|包邮|邮费|优惠|便宜|底价|最低')
_ADVICE = re.compile(r'(?:砍价|优惠|底价|降价).{0,6}(?:功能|设置|教程|提醒|怎么用)')


def classify_intent(message: str) -> Intent:
    text = str(message or '').strip().lower()
    # 按短句剔除否定/操作咨询，避免“别砍价”“砍价功能”制造事件。
    clauses = re.split(r'[，,。.!！?？;；\n]', text)
    bargaining = any(_BARGAIN.search(clause) and not _NEGATED.search(clause)
                     and not _ADVICE.search(clause) for clause in clauses)
    if bargaining:
        return Intent('price', True)
    if _TECH.search(text):
        return Intent('tech')
    if _PRICE.search(text):
        return Intent('price')
    return Intent('default')


def validate_scope(account_id, chat_id, rows):
    chat_id = normalize_chat(chat_id)
    if not account_id or not chat_id:
        raise ValueError('历史需要账号和会话身份')
    for row in rows:
        if str(row.get('account_id')) != str(account_id) or normalize_chat(row.get('chat_id', '')) != chat_id:
            raise ValueError('AI 历史的账号或会话归属不一致')


class BargainingHistory:
    def __init__(self, sessions=None):
        if sessions is None:
            from common.db.session import async_session_maker
            sessions = async_session_maker
        self.sessions = sessions

    async def reconcile_shared(self, account_id, chat_id):
        """计数不受 AI 的 100 条展示窗口限制；只读取 P2 原始 confirmed 买家事实。"""
        from common.models.reply_state import reply_events
        chat_id = normalize_chat(chat_id)
        cursor, added = 0, 0
        while True:
            # 已有议价事实不重复送入分类器；非议价历史只读，且用游标限制单批内存。
            recorded = select(BargainingEvent.id).where(
                BargainingEvent.account_id == reply_events.c.account_id,
                BargainingEvent.chat_id == reply_events.c.chat_id,
                BargainingEvent.event_id == reply_events.c.event_id).exists()
            query = select(reply_events).where(reply_events.c.account_id == str(account_id),
                reply_events.c.chat_id == chat_id, reply_events.c.cursor > cursor,
                reply_events.c.status == 'confirmed', reply_events.c.role == 'user',
                reply_events.c.origin == 'platform', reply_events.c.content_type == 'text', ~recorded
            ).order_by(reply_events.c.cursor).limit(200)
            async with self.sessions() as session:
                rows = [dict(row) for row in (await session.execute(query)).mappings()]
            if not rows:
                result = await self.record_confirmed(account_id, chat_id, [])
                result['new_events'] = added
                return result
            result = await self.record_confirmed(account_id, chat_id, rows)
            added += result['new_events']
            cursor = rows[-1]['cursor']

    async def record_confirmed(self, account_id, chat_id, rows):
        """平台事件是计数依据；发送成功、重试生成和历史长度都不是计数依据。"""
        validate_scope(account_id, chat_id, rows)
        chat_id = normalize_chat(chat_id)
        candidates = {}
        missing = 0
        for row in rows:
            if (row.get('status') != 'confirmed' or row.get('role') != 'user'
                    or row.get('origin') != 'platform' or row.get('content_type', 'text') != 'text'
                    or not classify_intent(row.get('content', '')).bargaining):
                continue
            event_id = str(row.get('event_id') or '')
            if not event_id.strip():
                missing += 1
                continue
            key = identity(str(account_id), chat_id, event_id)
            candidates.setdefault(key, (event_id, row))
        added = 0
        async with self.sessions() as session:
            for key, (event_id, row) in sorted(candidates.items()):
                if await session.get(BargainingEvent, key) is not None:
                    continue
                try:
                    async with session.begin_nested():
                        session.add(BargainingEvent(id=key, account_id=str(account_id), chat_id=chat_id,
                            event_id=event_id, content_hash=hashlib.sha256(str(row['content']).encode()).hexdigest(),
                            occurred_at=row.get('occurred_at')))
                        await session.flush()
                    added += 1
                except IntegrityError:
                    # 唯一键冲突由数据库裁决；其余完整性错误保留给调用方。
                    # MySQL 当前共享读：重复插入已持有共享锁，升级排他锁会使并发重放死锁。
                    existing = (await session.execute(select(BargainingEvent.id).where(
                        BargainingEvent.id == key).with_for_update(read=True))).scalar_one_or_none()
                    if existing is None:
                        raise
            await session.commit()
            count = (await session.execute(select(func.count()).select_from(BargainingEvent).where(
                BargainingEvent.account_id == str(account_id), BargainingEvent.chat_id == chat_id))).scalar_one()
        return {'count': count, 'new_events': added, 'missing_event_ids': missing}


def render_history(rows):
    return '\n'.join(f"{row['role']}: {row.get('content') or ''}" for row in rows)


def select_history(rows, budget: int):
    """保留连续的最近完整往来；整组裁剪，不截断一句人工承诺或最新消息。"""
    eligible = [dict(row) for row in rows if row.get('status') == 'confirmed'
                and row.get('role') in {'user', 'assistant'} and row.get('origin') != 'receipt'
                and row.get('content_type', 'text') == 'text']
    if eligible and all(row.get('cursor') is not None for row in eligible):
        eligible.sort(key=lambda row: row['cursor'])
    groups = []
    for row in eligible:
        if not groups or (row['role'] == 'user' and groups[-1][-1]['role'] == 'assistant'):
            groups.append([])
        groups[-1].append(row)
    selected_groups = []
    used = 0
    for group in reversed(groups):
        cost = len(render_history(group)) + (1 if selected_groups else 0)
        if used + cost > max(0, budget):
            if not selected_groups:
                raise ValueError('最新完整往来超出上下文预算')
            break
        selected_groups.insert(0, group)
        used += cost
    selected = [row for group in selected_groups for row in group]
    return selected, {
        'source': 'shared_confirmed', 'input_messages': len(rows), 'eligible_messages': len(eligible),
        'selected_messages': len(selected), 'trimmed_messages': len(eligible) - len(selected),
        'filtered_messages': len(rows) - len(eligible), 'selected_chars': used, 'budget_chars': max(0, budget),
        'manual_messages': sum(row.get('origin') == 'manual' for row in selected),
        'first_event_id': selected[0].get('event_id') if selected else None,
        'last_event_id': selected[-1].get('event_id') if selected else None,
        'first_cursor': selected[0].get('cursor') if selected else None,
        'last_cursor': selected[-1].get('cursor') if selected else None,
    }


def public_diagnostics(value):
    """仅投影业务诊断白名单，避免扩大已有日志接口的敏感快照暴露面。"""
    if not isinstance(value, dict) or not isinstance(value.get('bargaining'), dict):
        return {}
    result = {key: value[key] for key in ('stage', 'reason', 'call_id', 'config_version', 'trimmed_messages')
              if key in value and isinstance(value[key], (str, int, bool))}
    fields = {
        'bargaining': ('count', 'new_events', 'missing_event_ids', 'intent', 'is_bargaining'),
        'history_window': ('source', 'input_messages', 'eligible_messages', 'selected_messages',
            'trimmed_messages', 'filtered_messages', 'selected_chars', 'budget_chars', 'manual_messages',
            'first_event_id', 'last_event_id', 'first_cursor', 'last_cursor', 'reason'),
    }
    for group, keys in fields.items():
        source = value.get(group)
        if isinstance(source, dict):
            result[group] = {key: source[key] for key in keys if key in source
                             and (source[key] is None or isinstance(source[key], (str, int, bool)))}
    return result
