"""Shared-executor handler contract. Uses only injected live HTTP session."""
import importlib
from types import SimpleNamespace
import pytest
from test_monitor_reliability import store


def transport():
    try: return importlib.import_module('common.services.listing_monitor_transport')
    except ImportError as exc: pytest.fail(f'monitor account-dispatch handler missing: {exc}')

@pytest.mark.asyncio
@pytest.mark.parametrize('body,status,expected', [({'ret':['SUCCESS::OK'],'data':{'resultList':[]}}, 200, 'empty'),
    ({'ret':['SUCCESS::OK'],'data':{}}, 200, 'structure_error'),
    ({'ret':['FAIL_SYS_USER_VALIDATE'],'data':{}}, 200, 'platform_error'),
    ({}, 503, 'http_error')])
async def test_live_executor_http_retains_failures_and_binds_page(store, body, status, expected, monkeypatch):
    m, sessions = store
    t = transport(); monkeypatch.setattr(t, 'monitor_v2_enabled', lambda: True)
    svc = m.MonitorReliabilityService(sessions)
    await svc.begin(1, 'a'); ctx = await svc.page_context(1, 1)
    calls = []
    class Response:
        headers = {}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def json(self, **kw): return body
    Response.status = status
    class Session:
        def post(self, url, **kwargs):
            assert url == 'https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/'
            assert kwargs.get('allow_redirects') is False
            assert 'proxy' not in kwargs
            calls.append(kwargs); return Response()
    class Context:
        request = SimpleNamespace(owner_id=1, account_id='a', request_id=ctx['request_id'])
        store = SimpleNamespace(sessions=sessions)
        live = SimpleNamespace(session=Session())
        async def check(self): return SimpleNamespace(cookie='_m_h5_tk=fixture_1', account_id='a')
        async def before_external(self): calls.append('budget_and_fence')
    payload = {**ctx, 'monitor_type':'listing', 'rows_per_page':30, 'price_min':None, 'price_max':None, 'publish_days':None}
    result = await t.execute_monitor_search_page(Context(), payload)
    assert calls[0] == 'budget_and_fence'
    assert m.classify(result)[0] == expected
    assert all(result[k] == ctx[k] for k in m.IDENTITY)


def test_transport_schema_rejects_proxy_and_arbitrary_urls():
    t = transport()
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        t.MonitorSearchPage.model_validate({'proxy_url': 'https://proxy.test'})
