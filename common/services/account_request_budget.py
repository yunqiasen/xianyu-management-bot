"""平台业务共享预算（不是账号恢复两次预算）。Redis 故障及缺失策略均停止外发。"""
from __future__ import annotations

from dataclasses import dataclass
import math

# Redis TIME 避免 web / scheduler / WS 时钟差；账号键不带命令、配置版本或代次，接任不重置预算。
TAKE = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local last = tonumber(redis.call('HGET', KEYS[1], 'last') or '0')
local blocked = tonumber(redis.call('HGET', KEYS[1], 'blocked') or '0')
local interval = tonumber(ARGV[1])
local wait = math.max(blocked, last + interval) - now
if wait > 0 then return math.ceil(wait) end
redis.call('HSET', KEYS[1], 'last', now)
return 0
"""
DEFER = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local old = tonumber(redis.call('HGET', KEYS[1], 'blocked') or '0')
redis.call('HSET', KEYS[1], 'blocked', math.max(old, now + tonumber(ARGV[1])))
return 1
"""


class BudgetError(RuntimeError):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


@dataclass(frozen=True)
class RequestBudgetPolicy:
    interval_ms: int

    @classmethod
    def from_risk_config(cls, risk):
        """只取明确配置的合法频率；不自行加速，不借用 recovery_attempts。"""
        if not isinstance(risk, dict):
            raise BudgetError('risk_not_configured')
        intervals = []
        for key in ('min_interval_seconds', 'max_interval_seconds'):
            if key in risk:
                value = risk[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise BudgetError('risk_invalid')
                intervals.append(float(value))
        if 'requests_per_minute' in risk:
            value = risk['requests_per_minute']
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise BudgetError('risk_invalid')
            intervals.append(60 / value)
        if not intervals:
            raise BudgetError('risk_not_configured')
        if risk.get('max_interval_seconds', math.inf) < risk.get('min_interval_seconds', 0):
            raise BudgetError('risk_invalid')
        return cls(max(1, math.ceil(max(intervals) * 1000)))


def account_risk_config(account):
    """消费账号已提交配置快照。主程序可注入既有 risk 设置读取器，RPC 不接受预算参数。"""
    metadata = account.metadata_json or {}
    runtime = metadata.get('xy_runtime') or {}
    config = runtime.get('config_values') or {}
    return config.get('risk', metadata.get('risk'))


class AccountRequestBudget:
    def __init__(self, redis): self.redis = redis

    @staticmethod
    def key(account_id):
        return 'xymb:account:{' + str(account_id).encode().hex() + '}:business-budget'

    async def take(self, account_id, policy):
        if not isinstance(policy, RequestBudgetPolicy) or policy.interval_ms < 1:
            raise BudgetError('risk_invalid')
        try:
            wait = int(await self.redis.eval(TAKE, 1, self.key(account_id), policy.interval_ms))
        except Exception as exc:
            raise BudgetError('budget_unavailable') from exc
        if wait > 0:
            raise BudgetError('budget_wait', wait / 1000)

    async def acquire(self, account_id, policy, *, check, timeout=30):
        """Wait only before submission; recheck ownership while waiting and after Redis IO.

        Long cooldowns are returned to durable jobs instead of occupying an RPC forever.
        A timeout or cancellation here is evidence that this request has not been sent.
        """
        import asyncio
        import time
        deadline = time.monotonic() + max(0, timeout)
        while True:
            await check()
            try:
                await self.take(account_id, policy)
            except BudgetError as exc:
                if exc.code != 'budget_wait' or exc.retry_after is None:
                    raise
                if exc.retry_after > deadline - time.monotonic():
                    raise
                await asyncio.sleep(min(exc.retry_after, 1))
                continue
            await check()
            return

    async def defer(self, account_id, retry_after):
        if isinstance(retry_after, bool) or not isinstance(retry_after, (float, int)) or not math.isfinite(retry_after) or retry_after <= 0:
            raise BudgetError('invalid_retry_after')
        try:
            await self.redis.eval(DEFER, 1, self.key(account_id), math.ceil(retry_after * 1000))
        except Exception as exc:
            raise BudgetError('budget_unavailable') from exc


def parse_retry_after(value, *, now=None):
    """兼容平台秒数与 HTTP-date；非法提示不制造默认重试时间。"""
    import time
    from email.utils import parsedate_to_datetime
    if isinstance(value, bool) or value is None: return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try: seconds = parsedate_to_datetime(str(value)).timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError): return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None
