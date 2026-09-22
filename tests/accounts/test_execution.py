import asyncio
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).parents[2] / 'common/services/account_execution.py'
spec = importlib.util.spec_from_file_location('account_execution', path)
e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e)

class RedisBoundary:
    """Redis Lua 命令边界替身；真实 Redis 故障验收单列。"""
    def __init__(self): self.data = {}; self.generation = 0
    async def eval(self, script, count, *args):
        key = args[0]
        if script == e.ACQUIRE:
            if key in self.data: return 0
            self.generation += 1
            self.data[key] = f'{args[2]}:{self.generation}'
            return self.generation
        if self.data.get(key) != args[1]: return 0
        if script == e.RELEASE: self.data.pop(key)
        return 1
    async def get(self, key): return self.data.get(key)

class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_competing_workers_and_expired_generation(self):
        redis = RedisBoundary()
        first = e.AccountLease(redis, 'a'); second = e.AccountLease(redis, 'a')
        self.assertTrue(await first.acquire())
        self.assertFalse(await second.acquire())
        redis.data.clear()  # 模拟 TTL 到期 / 进程崩溃
        self.assertTrue(await second.acquire())
        self.assertGreater(second.generation, first.generation)
        with self.assertRaises(e.ExecutionLost): await first.check()
        self.assertFalse(await first.release())
        await second.check()

    async def test_renewal_failure_permanently_closes_old_lease(self):
        redis = RedisBoundary(); lease = e.AccountLease(redis, 'a')
        await lease.acquire(); redis.data.clear()
        self.assertFalse(await lease.renew())
        with self.assertRaises(e.ExecutionLost): await lease.check()

    async def test_socket_checks_before_every_send(self):
        from unittest.mock import AsyncMock
        raw = type('Socket', (), {})(); raw.send = AsyncMock()
        gate = AsyncMock(side_effect=[None, e.ExecutionLost('expired')])
        socket = e.FencedSocket(raw, gate)
        await socket.send('first')
        with self.assertRaises(e.ExecutionLost): await socket.send('late')
        raw.send.assert_awaited_once_with('first')
