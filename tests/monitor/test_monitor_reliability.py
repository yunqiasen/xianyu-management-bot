"""DEV46 S2/S3: real ORM, local SQLite, no account/browser/network."""
import importlib
from decimal import Decimal
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from common.db.base_class import Base
from common.models.listing_monitor_task import ListingMonitorTask


def api():
    try:
        return importlib.import_module('common.services.listing_monitor_reliability')
    except ImportError as exc:
        pytest.fail(f'DEV46 durable monitor service missing: {exc}')


@pytest.fixture
async def store(tmp_path):
    module = api()
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/monitor.db')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[ListingMonitorTask.__table__, *module.MONITOR_TABLES]))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add_all([ListingMonitorTask(id=i, owner_id=1, keyword='相机', collect_pages=2,
                   account_ids=['a'], is_enabled=True, monitor_type='listing') for i in (1, 2)])
        await db.commit()
    yield module, sessions
    await engine.dispose()


def response(ctx, items=(), **extra):
    return {**ctx, 'http_status': 200, 'ret': ['SUCCESS::调用成功'], 'items': list(items) if items is not None else None, **extra}


def item(id='one', price='100', area='杭州'):
    return {'item_id': id, 'price': price, 'title': '相机', 'area': area}


async def contexts(svc, task=1):
    await svc.begin(task, 'a')
    return [await svc.page_context(task, p) for p in (1, 2)]


@pytest.mark.asyncio
async def test_full_baseline_out_of_order_restart_and_dedup(store):
    m, sessions = store
    svc = m.MonitorReliabilityService(sessions)
    p1, p2 = await contexts(svc)
    assert (await svc.accept(p2, response(p2, [item('two')]))).status == 'partial'
    assert (await svc.status(1))['baseline_ready'] is False
    assert (await svc.status(1))['events'] == []
    svc = m.MonitorReliabilityService(sessions)  # restart from persisted checkpoint
    assert (await svc.page_context(1, 2)) == p2
    result = await svc.accept(p1, response(p1, [item(), item('two')]))
    assert result.status == 'baseline_complete'
    assert len((await svc.status(1))['items']) == 2
    assert (await svc.status(1))['events'] == []
    p3, p4 = await contexts(svc)
    assert (await svc.accept(p1, response(p1, [item('stale')]))).status == 'stale_response'
    await svc.accept(p3, response(p3, [item(), item('new')]))
    await svc.accept(p4, response(p4, [item('new')]))
    await svc.accept(p4, response(p4, [item('new')]))
    events = (await svc.status(1))['events']
    assert [(e['item_id'], e['kind']) for e in events] == [('new', 'listing')]


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('account_id', 'b'), ('task_id', 2), ('keyword', '手机'), ('page', 3), ('generation', 0), ('query_key', 'old'), ('request_id', 'old')])
async def test_all_response_identity_fields_checked(store, field, value):
    m, sessions = store
    svc = m.MonitorReliabilityService(sessions)
    p1, _ = await contexts(svc)
    assert (await svc.accept(p1, response(p1, [item()], **{field: value}))).status == 'stale_response'
    assert (await svc.status(1))['baseline_ready'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('extra,status', [({'http_status': 503}, 'http_error'), ({'ret': ['FAIL_SYS_SESSION_EXPIRED']}, 'platform_error'), ({'items': None}, 'structure_error'), ({'items': [{}]}, 'structure_error'), ({'ret': []}, 'structure_error')])
async def test_error_is_never_an_empty_baseline(store, extra, status):
    m, sessions = store
    svc = m.MonitorReliabilityService(sessions)
    p1, p2 = await contexts(svc)
    assert (await svc.accept(p1, response(p1, **extra))).status == status
    await svc.accept(p2, response(p2))
    assert (await svc.status(1))['baseline_ready'] is False
    retry = await svc.retry_page(1, 1)
    assert retry['request_id'] != p1['request_id']
    assert (await svc.accept(retry, response(retry))).status == 'baseline_complete'
    assert (await svc.status(1))['items'] == []


@pytest.mark.asyncio
async def test_price_rise_then_drop_and_task_scope(store):
    m, sessions = store
    async with sessions() as db:
        for i in (1, 2): (await db.get(ListingMonitorTask, i)).monitor_type = 'price_drop'
        await db.commit()
    svc = m.MonitorReliabilityService(sessions)
    for task in (1, 2):
        for price in ('100', '80', '100', '80'):
            p1, p2 = await contexts(svc, task)
            await svc.accept(p1, response(p1, [item(price=price)]))
            await svc.accept(p2, response(p2))
    for task in (1, 2):
        events = (await svc.status(task))['events']
        assert len(events) == 2
        assert all(e['kind'] == 'price_drop' for e in events)
    assert (await svc.status(1))['events'][0]['id'] != (await svc.status(2))['events'][0]['id']


@pytest.mark.asyncio
async def test_filters_changes_invalidate_inflight_and_rebaseline(store):
    m, sessions = store
    svc = m.MonitorReliabilityService(sessions)
    await svc.configure(1, region='杭州')
    async with sessions() as db:
        task = await db.get(ListingMonitorTask, 1)
        task.price_min, task.price_max = Decimal('50'), Decimal('150')
        await db.commit()
    p1, p2 = await contexts(svc)
    await svc.accept(p1, response(p1, [item('a'), item('b', area='上海'), item('c', price='200')]))
    await svc.accept(p2, response(p2))
    assert [i['item_id'] for i in (await svc.status(1))['items']] == ['a']
    p1, p2 = await contexts(svc)
    await svc.configure(1, region='上海')
    assert (await svc.accept(p1, response(p1, [item('late')]))).status == 'stale_response'
    p1, p2 = await contexts(svc)
    await svc.accept(p1, response(p1, [item('b', area='上海')]))
    await svc.accept(p2, response(p2))
    assert (await svc.status(1))['events'] == []


def test_feature_switch_defaults_off():
    m = api()
    from types import SimpleNamespace
    assert m.monitor_v2_enabled(SimpleNamespace()) is False
    assert m.monitor_v2_enabled(SimpleNamespace(xymb_enable_monitor_v2=False)) is False
    assert m.monitor_v2_enabled(SimpleNamespace(xymb_enable_monitor_v2='true')) is False
    assert m.monitor_v2_enabled(SimpleNamespace(xymb_enable_monitor_v2=True)) is True

@pytest.mark.asyncio
async def test_notifications_are_durable_retryable_and_unknown_not_resent(store):
    m, sessions = store
    from common.models.xy_account import XYAccount
    from common.models.notification_channel import NotificationChannel
    from common.models.message_notification import MessageNotification
    from common.models.notification_delivery import NotificationEvent, NotificationDelivery, NotificationTemplate
    from common.services.notification_delivery_service import NotificationDeliveryService
    async with sessions() as db:
        conn = await db.connection()
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[x.__table__ for x in
            (XYAccount, NotificationChannel, MessageNotification, NotificationEvent, NotificationDelivery, NotificationTemplate)]))
        db.add(XYAccount(id=1, owner_id=1, account_id='a', cookie='fixture', login_method='manual'))
        db.add(NotificationChannel(id=1, owner_id=1, name='fixture', channel_type='webhook', config_payload={}, enabled=True))
        db.add(MessageNotification(id=1, owner_id=1, account_pk=1, account_identifier='a', channel_id=1, enabled=True))
        await db.commit()
    svc = m.MonitorReliabilityService(sessions)
    for rows in ([], [item()]):
        p1, p2 = await contexts(svc)
        await svc.accept(p1, response(p1, rows)); await svc.accept(p2, response(p2))
    assert hasattr(svc, 'enqueue_notifications'), 'notification outbox bridge missing'
    from unittest.mock import AsyncMock, patch
    with patch.object(NotificationDeliveryService, 'publish', new=AsyncMock(side_effect=RuntimeError('fixture unavailable'))):
        await svc.enqueue_notifications(1)
    assert (await svc.status(1))['events'][0]['enqueue_status'] == 'enqueue_failed'
    assert (await svc.status(1))['baseline_ready'] is True
    await svc.enqueue_notifications(1)
    async with sessions() as db:
        events = (await db.execute(select(NotificationEvent))).scalars().all()
        assert len(events) == 1
        # Crash after publish but before monitor pointer update; time-window dedup alone is insufficient.
        event = (await db.execute(select(m.ListingMonitorEvent))).scalar_one()
        event.notification_event_id, event.enqueue_status = None, 'pending'
        events[0].first_seen = 0
        await db.commit()
    await m.MonitorReliabilityService(sessions).enqueue_notifications(1)
    now = [100000000000.0]; calls = []
    async def sender(*args):
        calls.append(args); return 'not_accepted' if len(calls) == 1 else 'unknown'
    async with sessions() as db:
        assert len((await db.execute(select(NotificationEvent))).scalars().all()) == 1
        delivery = NotificationDeliveryService(db, clock=lambda: now[0], sender=sender)
        await delivery.dispatch_due(); now[0] += 61
        await delivery.dispatch_due(); now[0] += 10000
        await delivery.dispatch_due()
        assert len(calls) == 2
        assert (await db.execute(select(NotificationDelivery.status))).scalar_one() == 'unknown'
    assert len((await svc.status(1))['events']) == 1


@pytest.mark.asyncio
async def test_dispatch_checkpoint_unknown_and_retry_budget(store):
    m, sessions = store
    from types import SimpleNamespace
    from common.models.xy_account import XYAccount
    async with sessions() as db:
        conn = await db.connection()
        await conn.run_sync(lambda c: XYAccount.__table__.create(c, checkfirst=True))
        db.add(XYAccount(id=1, owner_id=1, account_id='a', cookie='fixture', login_method='manual'))
        await db.commit()
    class Dispatcher:
        def __init__(self): self.calls = []; self.polls = []; self.saved = None
        async def submit(self, **kw):
            self.calls.append(kw); self.saved = kw['payload']
            return SimpleNamespace(status='unknown', result=None, error_code=None)
        async def get_operation(self, owner_id, account_id, request_id):
            self.polls.append(request_id)
            return SimpleNamespace(status='confirmed', result=response(self.saved), error_code=None)
    dispatcher = Dispatcher()
    assert hasattr(m, 'run_monitor_cycle'), 'scheduler dispatch integration missing'
    await m.run_monitor_cycle(1, sessions, dispatcher)
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0]['command'] == 'monitor_search_page'
    # Reboot resumes the existing operation, not a second account/browser request.
    await m.run_monitor_cycle(1, sessions, dispatcher)
    assert len(dispatcher.polls) == 1
    assert len(dispatcher.calls) == 2
    assert dispatcher.calls[1]['payload']['page'] == 2


@pytest.mark.asyncio
async def test_incompatible_proxy_stops_before_dispatch(store):
    m, sessions = store
    async with sessions() as db:
        task = await db.get(ListingMonitorTask, 1); task.proxy_url = 'https://proxy.invalid'
        await db.commit()
    svc = m.MonitorReliabilityService(sessions)
    with pytest.raises(ValueError, match='出口冲突'): await svc.begin(1, 'a')

@pytest.mark.asyncio
async def test_task_lease_fences_old_runner_and_allows_expired_restart(store):
    m, sessions = store
    svc = m.MonitorReliabilityService(sessions)
    assert await svc.lease(1, 'first', 100)
    assert not await svc.lease(1, 'second', 101)
    assert await svc.lease(1, 'second', 221)
    assert not await svc.renew(1, 'first', 222)
    await svc.release(1, 'first')
    assert not await svc.lease(1, 'third', 222)

@pytest.mark.asyncio
async def test_known_failed_page_late_old_attempt_never_satisfies_retry(store):
    m, sessions = store
    svc = m.MonitorReliabilityService(sessions)
    p1, _ = await contexts(svc)
    await svc.accept(p1, response(p1, http_status=503))
    retry = await svc.retry_page(1, 1)
    assert (await svc.accept(p1, response(p1, [item('old')]))).status == 'stale_response'
    assert (await svc.accept(retry, response(retry))).status == 'partial'

@pytest.mark.asyncio
async def test_generation_finalize_is_atomic_on_database_failure(store, monkeypatch):
    m, sessions = store
    svc = m.MonitorReliabilityService(sessions)
    p1, p2 = await contexts(svc)
    await svc.accept(p1, response(p1, [item('one')]))
    original = svc._finalize
    async def interrupted(*args):
        await original(*args)
        raise RuntimeError('simulated crash before commit')
    monkeypatch.setattr(svc, '_finalize', interrupted)
    with pytest.raises(RuntimeError): await svc.accept(p2, response(p2, [item('two')]))
    state = await svc.status(1)
    assert not state['baseline_ready'] and state['items'] == [] and state['events'] == []
    monkeypatch.setattr(svc, '_finalize', original)
    assert (await svc.accept(p2, response(p2, [item('two')]))).status == 'baseline_complete'


@pytest.mark.asyncio
async def test_duplicate_prices_resolve_by_page_not_arrival(store):
    m, sessions = store
    svc = m.MonitorReliabilityService(sessions)
    p1, p2 = await contexts(svc)
    await svc.accept(p2, response(p2, [item(price='90')]))
    await svc.accept(p1, response(p1, [item(price='100')]))
    assert (await svc.status(1))['items'][0]['price'] == '100'

@pytest.mark.asyncio
async def test_baseline_log_counters_are_real_observations_not_notifications(store):
    m, sessions = store
    svc = m.MonitorReliabilityService(sessions)
    p1, p2 = await contexts(svc)
    await svc.accept(p1, response(p1, [item()]))
    result = await svc.accept(p2, response(p2, [item('two')]))
    assert hasattr(result, 'fetched'), 'scan counters missing'
    assert (result.pages, result.fetched, result.inserted, result.updated, result.events) == (2, 2, 2, 0, 0)

@pytest.mark.asyncio
async def test_all_accounts_paused_waits_without_new_executor_or_dispatch(store):
    m, sessions = store
    from common.models.xy_account import XYAccount
    from unittest.mock import AsyncMock
    async with sessions() as db:
        conn = await db.connection()
        await conn.run_sync(lambda c: XYAccount.__table__.create(c, checkfirst=True))
        db.add(XYAccount(id=1, owner_id=1, account_id='a', cookie='fixture', login_method='manual', status='disabled'))
        await db.commit()
    dispatcher = AsyncMock()
    result = await m.run_monitor_cycle(1, sessions, dispatcher)
    assert result.status == 'waiting_account'
    dispatcher.submit.assert_not_called()
    assert (await m.MonitorReliabilityService(sessions).status(1))['phase'] == 'waiting_account'
