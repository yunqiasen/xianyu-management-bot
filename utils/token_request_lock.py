"""Adapt zhinianboke 0764f11 account-scoped token exclusion to single-host Docker.

Kernel file locks cover threads/processes sharing DB_PATH's directory, remain held
through long recovery, and release on crash. Never unlink lock files (inode race).
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import time


class TokenRequestLockError(RuntimeError):
    pass


@asynccontextmanager
async def token_request_lock(account_identifier, *, directory=None, wait_timeout=900):
    import fcntl
    identifier = str(account_identifier or '').strip()
    if not identifier:
        raise TokenRequestLockError('Token请求缺少账号标识')
    root = Path(directory) if directory is not None else Path(os.getenv('DB_PATH', 'data/xianyu_data.db')).parent / 'token_locks'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (hashlib.sha256(identifier.encode()).hexdigest() + '.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    acquired = False
    deadline = time.monotonic() + max(0, wait_timeout)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TokenRequestLockError('同账号Token请求仍在执行，请稍后重试')
                await asyncio.sleep(min(.1, max(0, deadline - time.monotonic())))
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
