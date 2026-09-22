import ast
import importlib.util
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, patch
import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'scheduler/app/services/scheduler/listing_monitor_task.py'


def test_legacy_business_function_kept_verbatim_from_fixed_baseline():
    original = subprocess.check_output(['git','show',f'fdc8eb039be771456ecfbbbe43fa26f57feb3947:{SOURCE}'], cwd=ROOT, text=True)
    before = next(n for n in ast.walk(ast.parse(original)) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_process_task')
    after = next(n for n in ast.walk(ast.parse((ROOT/SOURCE).read_text())) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_process_task_legacy')
    after.name = before.name
    assert ast.dump(before, include_attributes=False) == ast.dump(after, include_attributes=False)


@pytest.mark.asyncio
async def test_scheduler_switch_off_calls_legacy_only_and_on_never_falls_back():
    spec = importlib.util.spec_from_file_location('monitor_scheduler_under_test', ROOT/SOURCE)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    service = module.ListingMonitorTaskService()
    service._process_task_legacy = AsyncMock(return_value=None)
    service._process_task_v2 = AsyncMock(return_value='candidate')
    with patch('common.services.listing_monitor_reliability.monitor_v2_enabled', return_value=False):
        await service._process_task('fixture')
    service._process_task_legacy.assert_awaited_once()
    service._process_task_v2.assert_not_awaited()
    with patch('common.services.listing_monitor_reliability.monitor_v2_enabled', return_value=True):
        assert await service._process_task('fixture') == 'candidate'
    service._process_task_legacy.assert_awaited_once()
