import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from XianyuAutoAsync import XianyuLive


class TokenRequestLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_instances_of_same_account_do_not_refresh_concurrently(self):
        active = peak = 0
        async def refresh(*args, **kwargs):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            await asyncio.sleep(.06)
            active -= 1
        lives=[]
        for _ in range(2):
            live=XianyuLive.__new__(XianyuLive)
            live.cookie_id='same-account'; live.myid='same-account'
            live.token_refresh_lock=asyncio.Lock(); live.current_token=None
            live._should_skip_token_refresh_for_login_backoff=lambda:False
            live._refresh_token_impl=refresh
            lives.append(live)
        await asyncio.gather(*(live.refresh_token() for live in lives))
        self.assertEqual(peak,1,'Instance-only lock permits duplicate requests for the same account')

    async def test_lock_survives_owner_time_and_releases_on_cancel(self):
        from utils.token_request_lock import token_request_lock, TokenRequestLockError
        with tempfile.TemporaryDirectory() as d:
            entered=asyncio.Event()
            async def owner():
                async with token_request_lock('fixture', directory=d):
                    entered.set();await asyncio.sleep(10)
            task=asyncio.create_task(owner());await entered.wait()
            with self.assertRaises(TokenRequestLockError):
                async with token_request_lock('fixture', directory=d, wait_timeout=.05):pass
            async with token_request_lock('other-account', directory=d, wait_timeout=.05):pass
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            async with token_request_lock('fixture', directory=d, wait_timeout=.05):pass
            self.assertTrue(all('fixture' not in p.name for p in Path(d).iterdir()))

    async def test_other_process_waits_and_os_releases_crashed_owner(self):
        from utils.token_request_lock import token_request_lock, TokenRequestLockError
        with tempfile.TemporaryDirectory() as d:
            script='''import asyncio,sys
from utils.token_request_lock import token_request_lock
async def run():
 async with token_request_lock('fixture',directory=sys.argv[1]):
  print('READY',flush=True)
  await asyncio.sleep(100)
asyncio.run(run())'''
            child=subprocess.Popen([sys.executable,'-c',script,d],stdout=subprocess.PIPE,text=True)
            try:
                self.assertEqual(await asyncio.to_thread(child.stdout.readline),'READY\n')
                with self.assertRaises(TokenRequestLockError):
                    async with token_request_lock('fixture',directory=d,wait_timeout=.05):pass
            finally:
                child.kill();child.wait();child.stdout.close()
            async with token_request_lock('fixture',directory=d,wait_timeout=.1):pass
