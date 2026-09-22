"""Redis 原子租约；失去执行权后该实例永久关闭，接任须创建新租约。"""
from __future__ import annotations

import secrets
from contextvars import ContextVar

OUTBOUND_GUARD = ContextVar("account_outbound_guard", default=None)

ACQUIRE = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
local generation = redis.call('INCR', KEYS[2])
redis.call('PSETEX', KEYS[1], ARGV[2], ARGV[1] .. ':' .. generation)
return generation
"""
RENEW = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('PEXPIRE', KEYS[1], ARGV[2])
"""
RELEASE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


class ExecutionLost(RuntimeError):
    pass


class AccountLease:
    def __init__(self, redis, account_id, *, ttl_ms=30000):
        if ttl_ms < 1000:
            raise ValueError('租期至少 1000ms')
        self.redis = redis
        # hash tag 使 Redis Cluster 中的两个 key 同槽。
        identity = str(account_id).encode().hex()
        self.key = f'xymb:account:{{{identity}}}:lease'
        self.generation_key = f'xymb:account:{{{identity}}}:generation'
        self.owner = secrets.token_hex(16)
        self.ttl_ms = ttl_ms
        self.generation = 0
        self.lost = False

    @property
    def token(self):
        return f'{self.owner}:{self.generation}'

    async def acquire(self):
        if self.lost or self.generation:
            return False
        try:
            generation = await self.redis.eval(ACQUIRE, 2, self.key, self.generation_key,
                                               self.owner, self.ttl_ms)
        except Exception as exc:
            self.lost = True
            raise ExecutionLost('执行权存储不可用') from exc
        self.generation = int(generation)
        return self.generation > 0

    async def check(self):
        if self.lost or not self.generation:
            raise ExecutionLost('执行权已失效')
        try:
            value = await self.redis.get(self.key)
            if isinstance(value, bytes):
                value = value.decode()
            if value == self.token:
                return
        except Exception as exc:
            self.lost = True
            raise ExecutionLost('执行权核验失败') from exc
        self.lost = True
        raise ExecutionLost('执行权已被接任')

    async def renew(self):
        if self.lost or not self.generation:
            return False
        try:
            ok = bool(await self.redis.eval(RENEW, 1, self.key, self.token, self.ttl_ms))
        except Exception:
            ok = False
        if not ok:
            self.lost = True
        return ok

    async def release(self):
        self.lost = True
        if not self.generation:
            return False
        try:
            return bool(await self.redis.eval(RELEASE, 1, self.key, self.token))
        except Exception:
            return False


class FencedSocket:
    """保留原连接协议；每次外发前校验当前执行权和账号版本。"""
    def __init__(self, socket, check, *, before_send=None):
        self._socket, self._check, self._before_send = socket, check, before_send

    def __getattr__(self, key):
        return getattr(self._socket, key)

    def __aiter__(self):
        return self._socket.__aiter__()

    async def send(self, *args, **kwargs):
        await self._check()
        if self._before_send is not None:
            await self._before_send(*args, **kwargs)
            await self._check()
        guard = OUTBOUND_GUARD.get()
        if guard is not None:
            await guard()
            await self._check()
        return await self._socket.send(*args, **kwargs)
