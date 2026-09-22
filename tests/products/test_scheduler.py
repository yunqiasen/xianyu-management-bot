import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from stdlib_loader import load

class SchedulerEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_rejects_wrong_owner_before_remote_eligibility(self):
        m=load('scheduler/app/services/scheduler/rate_task.py',asyncio=asyncio)
        account=SimpleNamespace(id=1,account_id='a1',owner_id=1,cookie='fixture',status='active')
        order=SimpleNamespace(account_id='a2',owner_id=2,is_rated=False,status='completed')
        result=await m.RateTask()._process_order(account,order)
        self.assertEqual(result,(False,'skipped:account_mismatch','fixture'))

    async def test_flower_account_entry_logs_skip_without_send(self):
        m=load('scheduler/app/services/scheduler/red_flower_task.py',asyncio=asyncio,
               ORDER_COOLDOWN_SECONDS=600, _order_cooldown_cache={}, is_account_session_cooled=lambda _:False,is_order_in_cooldown=lambda _:False)
        task=m.RedFlowerTask()
        task._get_pending_orders=AsyncMock(return_value=[SimpleNamespace(account_id='a1',owner_id=1,
            order_no='o1',is_red_flower=True,status='completed')])
        task._request_red_flower=AsyncMock()
        task._log_result=AsyncMock()
        account=SimpleNamespace(account_id='a1',owner_id=1,cookie='fixture',status='active',metadata_json={})
        await task._process_account(SimpleNamespace(), 'batch', account)
        task._request_red_flower.assert_not_awaited()
        self.assertEqual(task._log_result.call_args.args[-1],'skipped:already_processed')

    async def test_recovering_account_does_not_spend_polish_cycle(self):
        m=load('scheduler/app/services/scheduler/polish_task.py',asyncio=asyncio)
        task=m.PolishTaskService();task._log_execution=AsyncMock();task._get_unpolished_items=AsyncMock()
        account=SimpleNamespace(account_id='a1',status='active',metadata_json={'xy_runtime':{'business_state':'recovering'}})
        self.assertEqual(await task._process_account(SimpleNamespace(),account,'batch'),(0,0))
        task._get_unpolished_items.assert_not_awaited()
        self.assertIn('skipped:recovering',task._log_execution.call_args.args[-1])
