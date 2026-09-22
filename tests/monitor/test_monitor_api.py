"""S1 actual FastAPI router + ORM; no running service required."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy import BigInteger
from test_monitor_reliability import store

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend-web'))
from app.api.routes import listing_monitor as routes
from common.models.listing_monitor_task import ListingMonitorTask
from common.models.listing_monitor_category import ListingMonitorCategory

@compiles(BigInteger, 'sqlite')
def sqlite_bigint(type_, compiler, **kw): return 'INTEGER'

@pytest.fixture
async def client(store):
    m, sessions = store
    async with sessions() as db:
        conn = await db.connection()
        await conn.run_sync(lambda c: ListingMonitorCategory.__table__.create(c, checkfirst=True))
        db.add(ListingMonitorCategory(id=1, owner_id=1, name='相机'))
        (await db.get(ListingMonitorTask, 2)).owner_id = 2
        await db.commit()
    app = FastAPI(); app.include_router(routes.router)
    async def db_dependency():
        async with sessions() as db: yield db
    app.dependency_overrides[routes.get_db_session] = db_dependency
    app.dependency_overrides[routes.get_current_active_user] = lambda: SimpleNamespace(id=1, role='USER')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        yield client, m, sessions

PREFIX = '/product-monitor/listing-tasks'

@pytest.mark.asyncio
async def test_new_disabled_five_minutes_one_page_and_existing_unchanged(client):
    http, m, sessions = client
    resp = await http.post(PREFIX, json={'monitor_type':'listing', 'category_id':1, 'keyword':'镜头'})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['success'], data
    task = data['data']['task']
    assert (task['is_enabled'], task['interval_minutes'], task['collect_pages']) == (False, 5, 1)
    async with sessions() as db:
        assert (await db.get(ListingMonitorTask, 1)).is_enabled is True

@pytest.mark.asyncio
async def test_reliability_status_gate_ownership_and_region(client):
    http, m, sessions = client
    with patch.object(m, 'monitor_v2_enabled', return_value=False):
        response = await http.get(f'{PREFIX}/1/reliability')
        assert response.status_code == 200, response.text
        assert response.json()['data']['enabled'] is False
        response = await http.put(f'{PREFIX}/1/reliability', json={'region':'杭州'})
        assert response.json()['success'] is False
    with patch.object(m, 'monitor_v2_enabled', return_value=True):
        response = await http.put(f'{PREFIX}/1/reliability', json={'region':'杭州'})
        assert response.json()['success'], response.text
        response = await http.get(f'{PREFIX}/1/reliability')
        assert response.json()['data']['region'] == '杭州'
        assert response.json()['data']['baseline_ready'] is False
        response = await http.get(f'{PREFIX}/2/reliability')
        assert response.json()['success'] is False

@pytest.mark.asyncio
async def test_feature_off_preserves_update_values_and_manual_scheduler_path(client):
    http, m, sessions = client
    with patch.object(m, 'monitor_v2_enabled', return_value=False):
        response = await http.put(f'{PREFIX}/1', json={'interval_minutes':1, 'collect_pages':3, 'is_enabled':True})
        assert response.json()['success'], response.text
        assert response.json()['data']['task']['interval_minutes'] == 1
        class HTTP:
            async def post(self, url, **kwargs):
                assert url.endswith('/internal/tasks/listing_monitor/run/1')
                return {'success': True, 'message': 'legacy scheduler'}
        with patch.object(routes, 'get_http_client', return_value=HTTP()):
            response = await http.post(f'{PREFIX}/1/run')
        assert response.json()['message'] == 'legacy scheduler'

@pytest.mark.asyncio
async def test_candidate_rejects_enabling_conflicting_old_task_without_mutating_it(client):
    http, m, sessions = client
    async with sessions() as db:
        task = await db.get(ListingMonitorTask, 1)
        task.proxy_url, task.is_enabled = 'https://proxy.invalid', False
        await db.commit()
    with patch.object(m, 'monitor_v2_enabled', return_value=True):
        result = (await http.put(f'{PREFIX}/1/status', json={'is_enabled':True})).json()
        assert result['success'] is False
    async with sessions() as db:
        assert (await db.get(ListingMonitorTask, 1)).is_enabled is False


@pytest.mark.asyncio
async def test_off_gate_still_allows_legacy_task_proxy_enable(client):
    http, m, sessions = client
    async with sessions() as db:
        task = await db.get(ListingMonitorTask, 1)
        task.proxy_url, task.is_enabled = 'https://proxy.invalid', False
        await db.commit()
    with patch.object(m, 'monitor_v2_enabled', return_value=False):
        result = (await http.put(f'{PREFIX}/1/status', json={'is_enabled':True})).json()
        assert result['success'] is True
