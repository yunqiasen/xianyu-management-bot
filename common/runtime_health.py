"""Dependency health shared by every service; never includes exception secrets."""
import asyncio
from common.runtime_version import build_identity

async def _database_probe():
    from common.db.session import async_engine
    from sqlalchemy import text
    async with async_engine.connect() as conn:
        await conn.execute(text('SELECT 1'))
    return True

async def _redis_probe():
    from common.db.redis_client import get_redis_client
    return await (await get_redis_client()).ping()

async def _probe(fn):
    try:
        return bool(await asyncio.wait_for(fn(), timeout=3))
    except Exception:
        return False

async def service_health(service, workers_enabled, database_probe=None, redis_probe=None):
    database, redis = await asyncio.gather(_probe(database_probe or _database_probe), _probe(redis_probe or _redis_probe))
    ready = database and redis
    return {
        'success': ready, 'code': 200 if ready else 503,
        'message': '服务运行正常' if ready else '依赖异常',
        'data': {
            'service': service, **build_identity(),
            'status': 'running' if ready else 'degraded',
            'database': 'connected' if database else 'disconnected',
            'redis': 'connected' if redis else 'disconnected',
            'workers_enabled': bool(workers_enabled),
        },
    }
