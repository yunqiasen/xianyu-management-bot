"""双回复顺序与有界过滤。由业务入口调用，不包含平台通信。"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Awaitable, Callable, Literal


@dataclass(frozen=True)
class ReplyDecision:
    kind: Literal['unmatched', 'body', 'skip', 'error']
    source: str
    content: Any = None
    rule_id: str | None = None
    version: int | None = None


async def select_reply(strategy: str, provider: Callable[[str], Awaitable[ReplyDecision]]) -> ReplyDecision:
    if strategy not in {'legacy', 'ai_first'}:
        raise ValueError('未知回复策略')
    tail = ('default', 'ai') if strategy == 'legacy' else ('ai', 'default')
    last = ReplyDecision('unmatched', 'none')
    for name in ('exclusive', 'keyword', *tail):
        decision = await provider(name)
        if decision.kind in {'body', 'skip'}:
            return decision
        if decision.kind == 'error':
            last = decision
    return last


ACTIONS = {'notify', 'skip_ai', 'skip_reply', 'pause', 'skip_notify'}


def validate_filter(rule: dict) -> dict:
    rule = dict(rule)
    pattern = rule.get('pattern', '')
    if not isinstance(pattern, str) or not pattern or len(pattern) > 256:
        raise ValueError('过滤内容长度应为1至256')
    mode = rule.get('match_mode', 'contains')
    if mode not in {'contains', 'exact', 'regex'}:
        raise ValueError('未知匹配方式')
    if rule.get('source', 'user') not in {'user', 'system', 'ai', 'all'}:
        raise ValueError('未知消息来源')
    actions = rule.get('actions', [])
    if not actions or not set(actions) <= ACTIONS:
        raise ValueError('未知过滤动作')
    pause=rule.get('pause_minutes')
    if pause is not None and (type(pause) is not int or not 0<=pause<=1440):
        raise ValueError('暂停分钟数应为0至1440或留空继承账号')
    if mode == 'regex':
        # 限定线性子集：无组、分支、反向引用、计数重复；至多一个无界重复。
        # 既拒绝配置错误，也避免依赖线程超时（线程不会停止正则执行）。
        if any(c in pattern for c in '()|{}') or re.search(r'\\[1-9]', pattern) or len(re.findall(r'(?<!\\)[*+?]', pattern)) > 1:
            raise ValueError('正则仅支持无分组无分支的简单模式和一个重复项')
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError('正则格式错误') from exc
    rule.setdefault('enabled', True)
    return rule


@dataclass(frozen=True)
class FilterDecision:
    actions: frozenset[str]
    rule_ids: tuple
    pause_minutes: int = 0
    inherit_pause: bool = False

    @property
    def blocks_reply(self) -> bool:
        return bool(self.actions & {'skip_reply', 'pause'})


def evaluate_filters(rules: list[dict], content: str, source: str, item_id: str) -> FilterDecision:
    actions, matched = set(), []
    minutes, inherit = 0, False
    for raw in rules:
        if not raw.get('enabled', True) or raw.get('source', 'user') not in {source, 'all'}:
            continue
        if raw.get('item_id') and raw['item_id'] != item_id:
            continue
        try:
            rule = validate_filter(raw)
        except ValueError:
            continue  # 旧非法配置隔离，不阻塞消息服务
        pattern, mode = rule['pattern'], rule.get('match_mode', 'contains')
        hit = (pattern in content if mode == 'contains' else content == pattern if mode == 'exact'
               else bool(re.search(pattern, content[:8192])))
        if hit:
            actions.update(rule['actions'])
            matched.append(rule.get('id'))
            if 'pause' in rule['actions']:
                if rule.get('pause_minutes') is None: inherit=True
                else: minutes=max(minutes,rule['pause_minutes'])
    return FilterDecision(frozenset(actions), tuple(matched), minutes, inherit)
