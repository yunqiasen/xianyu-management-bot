"""Persist full scan generations before generating notifications (DEV46 candidate).

All transactions lock the existing task first: no process-local dedup or cross-task
item identity. Failed pages never satisfy the baseline. No platform IO in this module.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from sqlalchemy import select
from common.models.listing_monitor_task import ListingMonitorTask
from common.models.listing_monitor_reliability import (
    ListingMonitorState, ListingMonitorPage, ListingMonitorObservation, ListingMonitorEvent,
)

MONITOR_TABLES = [m.__table__ for m in (ListingMonitorState, ListingMonitorPage,
                                     ListingMonitorObservation, ListingMonitorEvent)]
IDENTITY = ('task_id', 'account_id', 'keyword', 'page', 'generation', 'query_key', 'request_id')
COMPLETE = {'complete', 'empty'}
ERRORS = {'http_error', 'platform_error', 'structure_error', 'dispatch_failed'}


def monitor_v2_enabled(settings=None):
    if settings is None:
        from common.core.config import get_settings
        settings = get_settings()
    return getattr(settings, 'xymb_enable_monitor_v2', False) is True


def key(*values):
    return sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def query_key(task, region):
    return key(task.keyword, task.monitor_type, task.price_min, task.price_max, task.publish_days,
               task.collect_pages, task.account_ids, region)


def context(page):
    return {name: getattr(page, name) for name in IDENTITY}


def classify(payload):
    if not isinstance(payload, dict): return 'structure_error', []
    status = payload.get('http_status')
    if type(status) is not int: return 'structure_error', []
    if status < 200 or status >= 300: return 'http_error', []
    ret = payload.get('ret')
    if not isinstance(ret, list) or not ret or not all(isinstance(v, str) for v in ret):
        return 'structure_error', []
    if not all(v == 'SUCCESS' or v.startswith('SUCCESS::') for v in ret):
        return 'platform_error', []
    items = payload.get('items')
    if not isinstance(items, list): return 'structure_error', []
    clean = []
    for item in items:
        if not isinstance(item, dict): return 'structure_error', []
        iid = item.get('item_id')
        if not isinstance(iid, str) or not iid or len(iid) > 64: return 'structure_error', []
        try:
            price = Decimal(str(item['price']))
            if not price.is_finite() or price < 0 or price > Decimal('9999999999.99'):
                return 'structure_error', []
        except (KeyError, InvalidOperation, ValueError): return 'structure_error', []
        # Store a display allowlist, not raw platform data / credentials.
        clean.append({'item_id': iid, 'price': str(price),
                      'title': str(item.get('title') or '')[:500],
                      'area': str(item.get('area') or '')[:120]})
    return ('complete' if clean else 'empty'), clean


@dataclass(frozen=True)
class ScanResult:
    status: str
    events: int = 0
    pages: int = 0
    fetched: int = 0
    inserted: int = 0
    updated: int = 0


class MonitorReliabilityService:
    def __init__(self, sessions):
        self.sessions = sessions

    async def _task(self, db, task_id):
        task = (await db.execute(select(ListingMonitorTask).where(ListingMonitorTask.id == task_id)
                                .with_for_update())).scalar_one_or_none()
        if not task or task.is_deleted: raise ValueError('监控任务不存在')
        return task

    async def _state(self, db, task):
        state = await db.get(ListingMonitorState, task.id)
        if state is None:
            state = ListingMonitorState(task_id=task.id, owner_id=task.owner_id, region='',
                query_key='', generation=0, expected_pages=1, baseline_ready=False, phase='pending',
                lease_until=0, lease_token='')
            db.add(state)
            await db.flush()
        return state

    async def configure(self, task_id, *, region):
        region = region.strip()
        if len(region) > 120: raise ValueError('地区最长120字符')
        async with self.sessions() as db:
            task = await self._task(db, task_id)
            state = await self._state(db, task)
            if state.region != region:
                state.region, state.baseline_ready, state.phase = region, False, 'pending'
                state.query_key = ''
            await db.commit()

    async def begin(self, task_id, account_id):
        async with self.sessions() as db:
            task = await self._task(db, task_id)
            if not task.is_enabled: raise ValueError('监控任务已停用')
            if account_id not in (task.account_ids or []): raise ValueError('采集账号不属于任务')
            if task.proxy_url: raise ValueError('任务代理与固定账号出口冲突')
            if task.direct_order or task.order_account_ids or task.dm_content:
                raise ValueError('增强监控只发现和通知；请先停用该任务的下单及私信配置')
            state = await self._state(db, task)
            fingerprint = query_key(task, state.region)
            if state.query_key == fingerprint and state.phase != 'complete':
                completed = (await db.execute(select(ListingMonitorPage.status).where(
                    ListingMonitorPage.task_id == task.id, ListingMonitorPage.generation == state.generation
                ))).scalars().all()
                if len(completed) != state.expected_pages or any(p not in COMPLETE for p in completed):
                    await db.commit()
                    return state.generation
            if state.query_key != fingerprint:
                state.baseline_ready = False
            state.query_key, state.generation = fingerprint, state.generation + 1
            state.expected_pages, state.phase = task.collect_pages, 'collecting'
            for p in range(1, task.collect_pages + 1):
                id_ = key(task.id, state.generation, p)
                db.add(ListingMonitorPage(id=id_, task_id=task.id, generation=state.generation,
                    page=p, account_id=account_id, keyword=task.keyword, query_key=fingerprint,
                    request_id=f'mon:{id_}:1', attempt=1, status='pending', items=[]))
            await db.commit()
            return state.generation

    async def page_context(self, task_id, page):
        async with self.sessions() as db:
            state = await db.get(ListingMonitorState, task_id)
            row = await db.get(ListingMonitorPage, key(task_id, state.generation, page))
            return context(row)

    async def retry_page(self, task_id, page):
        async with self.sessions() as db:
            await self._task(db, task_id)
            state = await db.get(ListingMonitorState, task_id)
            row = await db.get(ListingMonitorPage, key(task_id, state.generation, page))
            if row.status not in ERRORS: raise ValueError('只重试明确失败页；待核实页保留原请求')
            row.attempt += 1
            row.request_id, row.status = f'mon:{row.id}:{row.attempt}', 'pending'
            await db.commit()
            return context(row)

    async def accept(self, expected, payload):
        if not isinstance(payload, dict) or any(payload.get(k) != expected.get(k) for k in IDENTITY):
            return ScanResult('stale_response')
        async with self.sessions() as db:
            task = await self._task(db, expected['task_id'])
            state = await db.get(ListingMonitorState, task.id)
            if (not state or not task.is_enabled or expected['generation'] != state.generation
                    or expected['query_key'] != query_key(task, state.region)
                    or state.query_key != expected['query_key']):
                return ScanResult('stale_response')
            page = await db.get(ListingMonitorPage, key(task.id, state.generation, expected['page']))
            if page is None or context(page) != expected: return ScanResult('stale_response')
            if page.status in COMPLETE: return ScanResult('duplicate_response')
            page.status, page.items = classify(payload)
            if page.status not in COMPLETE:
                state.phase = page.status
                await db.commit()
                return ScanResult(page.status)
            await db.flush()
            pages = (await db.execute(select(ListingMonitorPage).where(
                ListingMonitorPage.task_id == task.id, ListingMonitorPage.generation == state.generation
            ).order_by(ListingMonitorPage.page))).scalars().all()
            if len(pages) != state.expected_pages or any(p.status not in COMPLETE for p in pages):
                state.phase = 'partial'
                await db.commit()
                return ScanResult('partial')
            result = await self._finalize(db, task, state, pages)
            await db.commit()
            return result

    async def _finalize(self, db, task, state, pages):
        # Earliest search page wins duplicate observations regardless of arrival order.
        items = {}
        for page in pages:
            for item in page.items: items.setdefault(item['item_id'], (item, page.account_id))
        count = inserted = updated = 0
        for iid, (item, account) in items.items():
            price = Decimal(item['price'])
            matches = (not state.region or state.region in item['area']) and (
                task.price_min is None or price >= task.price_min) and (
                task.price_max is None or price <= task.price_max)
            observation_id = key(task.id, state.query_key, iid)
            previous = await db.get(ListingMonitorObservation, observation_id)
            old_price = previous.price if previous else None
            revision = previous.revision if previous else 0
            changed = old_price is not None and Decimal(old_price) != price
            kind = ('listing' if previous is None and task.monitor_type == 'listing' else
                    'price_drop' if old_price is not None and price < Decimal(old_price)
                    and task.monitor_type == 'price_drop' else None)
            if not previous:
                inserted += 1
                previous = ListingMonitorObservation(id=observation_id, task_id=task.id,
                    query_key=state.query_key, item_id=iid, price=item['price'], revision=1,
                    data={**item, 'matches': matches})
                db.add(previous)
            else:
                updated += 1
                previous.price, previous.data = item['price'], {**item, 'matches': matches}
                if changed: previous.revision = revision + 1
            if state.baseline_ready and kind and matches:
                identity = key(observation_id, previous.revision, kind)
                # Existing observations are kept across generations: same item in another task is independent.
                if await db.get(ListingMonitorEvent, identity) is None:
                    summary = f'监控任务{task.id} [{task.keyword}] {kind}：{item["title"]}，商品{iid}，{old_price or "-"} → {item["price"]}，{item["area"]}'
                    db.add(ListingMonitorEvent(id=identity, task_id=task.id, owner_id=task.owner_id,
                        account_id=account, item_id=iid, generation=state.generation, kind=kind,
                        old_price=old_price, price=item['price'], summary=summary[:1000]))
                    count += 1
        was_ready = state.baseline_ready
        state.baseline_ready, state.phase = True, 'complete'
        return ScanResult('complete' if was_ready else 'baseline_complete', count,
                          len(pages), sum(len(p.items) for p in pages), inserted, updated)

    async def status(self, task_id):
        async with self.sessions() as db:
            task = await self._task(db, task_id)
            state = await db.get(ListingMonitorState, task_id)
            if state is None:
                return dict(baseline_ready=False, phase='pending', generation=0, region='', pages=[], events=[], items=[])
            pages = (await db.execute(select(ListingMonitorPage).where(ListingMonitorPage.task_id == task_id,
                ListingMonitorPage.generation == state.generation).order_by(ListingMonitorPage.page))).scalars().all()
            events = (await db.execute(select(ListingMonitorEvent).where(ListingMonitorEvent.task_id == task_id)
                .order_by(ListingMonitorEvent.generation.desc(), ListingMonitorEvent.id).limit(100))).scalars().all()
            items = (await db.execute(select(ListingMonitorObservation).where(ListingMonitorObservation.task_id == task_id,
                ListingMonitorObservation.query_key == state.query_key).order_by(ListingMonitorObservation.item_id).limit(200))).scalars().all()
            fresh = state.query_key == query_key(task, state.region)
            return dict(baseline_ready=state.baseline_ready and fresh, phase=state.phase if fresh or state.phase == 'waiting_account' else 'pending',
                generation=state.generation, region=state.region, expected_pages=state.expected_pages,
                pages=[{**context(p), 'status': p.status} for p in pages],
                events=[{name: getattr(e, name) for name in ('id', 'item_id', 'kind', 'price', 'old_price',
                    'generation', 'summary', 'enqueue_status', 'notification_event_id')} for e in events],
                items=[i.data for i in items if i.data.get('matches')])

    async def enqueue_notifications(self, task_id):
        """Business outbox -> existing notification service; never send network here.

        Query the permanent generation identity before publish, including after the
        shared service's merge window. Its commit and our pointer commit may be split
        by a crash; the next run finds the persisted notification instead of resending.
        """
        from common.models.notification_delivery import NotificationEvent
        from common.services.notification_delivery_service import NotificationDeliveryService
        async with self.sessions() as db:
            ids = (await db.execute(select(ListingMonitorEvent.id).where(
                ListingMonitorEvent.task_id == task_id, ListingMonitorEvent.notification_event_id.is_(None))
                .limit(100))).scalars().all()
        for event_id in ids:
            async with self.sessions() as db:
                event = (await db.execute(select(ListingMonitorEvent).where(ListingMonitorEvent.id == event_id)
                                         .with_for_update())).scalar_one()
                if event.notification_event_id: continue
                try:
                    notification = (await db.execute(select(NotificationEvent).where(
                        NotificationEvent.owner_id == event.owner_id,
                        NotificationEvent.account_id == event.account_id,
                        NotificationEvent.event_type == 'message',
                        NotificationEvent.generation == event.id).limit(1))).scalar_one_or_none()
                    if notification is None:
                        notification = await NotificationDeliveryService(db).publish(event.owner_id,
                            event.account_id, 'message', event.id, {'summary': event.summary})
                    event.notification_event_id, event.enqueue_status = notification.id, 'queued'
                    await db.commit()
                except Exception:
                    await db.rollback()
                    event = await db.get(ListingMonitorEvent, event_id)
                    event.enqueue_status = 'enqueue_failed'
                    await db.commit()

    async def page_status(self, task_id, page_number):
        async with self.sessions() as db:
            state = await db.get(ListingMonitorState, task_id)
            page = await db.get(ListingMonitorPage, key(task_id, state.generation, page_number))
            return page.status

    async def mark_page(self, ctx, status):
        async with self.sessions() as db:
            await self._task(db, ctx['task_id'])
            state = await db.get(ListingMonitorState, ctx['task_id'])
            page = await db.get(ListingMonitorPage, key(ctx['task_id'], ctx['generation'], ctx['page']))
            if page and state.generation == ctx['generation'] and context(page) == ctx and page.status not in COMPLETE:
                page.status, state.phase = status, status
                await db.commit()

    async def lease(self, task_id, token, now):
        async with self.sessions() as db:
            task = await self._task(db, task_id)
            state = await self._state(db, task)
            if state.lease_until > now: return False
            state.lease_token, state.lease_until = token, now + 120
            await db.commit()
            return True

    async def release(self, task_id, token):
        async with self.sessions() as db:
            await self._task(db, task_id)
            state = await db.get(ListingMonitorState, task_id)
            if state.lease_token == token:
                state.lease_until, state.lease_token = 0, ''
                await db.commit()

    async def renew(self, task_id, token, now):
        async with self.sessions() as db:
            await self._task(db, task_id)
            state = await db.get(ListingMonitorState, task_id)
            if state.lease_token != token or state.lease_until <= now: return False
            state.lease_until = now + 120
            await db.commit()
            return True


async def run_monitor_cycle(task_id, sessions, dispatcher):
    """Scheduler integration; only existing account dispatcher may touch the platform."""
    import time
    from uuid import uuid4
    from common.models.xy_account import XYAccount
    from common.services.account_policy import snapshot, INACTIVE
    from common.services.account_dispatch import DispatchError
    svc = MonitorReliabilityService(sessions)
    token = str(uuid4())
    if not await svc.lease(task_id, token, time.time()): return ScanResult('busy')
    try:
        async with sessions() as db:
            task = await svc._task(db, task_id)
            accounts = (await db.execute(select(XYAccount).where(XYAccount.owner_id == task.owner_id,
                XYAccount.account_id.in_(task.account_ids or [])))).scalars().all()
            by_id = {a.account_id: a for a in accounts}
            account_id = next((aid for aid in (task.account_ids or []) if aid in by_id
                and by_id[aid].status not in INACTIVE and not snapshot(by_id[aid])['recovery_running']
                and snapshot(by_id[aid])['next_retry_at'] <= time.time()), None)
            if account_id is None:
                state = await svc._state(db, task)
                state.phase = 'waiting_account'
                await db.commit()
                return ScanResult('waiting_account')
        await svc.begin(task_id, account_id)
        await svc.enqueue_notifications(task_id)  # repair a previous enqueue failure even if this scan fails
        result = ScanResult('partial')
        for p in range(1, task.collect_pages + 1):
            if not await svc.renew(task_id, token, time.time()): return ScanResult('lease_lost')
            phase = await svc.page_status(task_id, p)
            if phase in COMPLETE: continue
            ctx = await svc.page_context(task_id, p)
            if phase in ERRORS: ctx = await svc.retry_page(task_id, p); phase = 'pending'
            payload = {**ctx, 'monitor_type': task.monitor_type,
                'price_min': str(task.price_min) if task.price_min is not None else None,
                'price_max': str(task.price_max) if task.price_max is not None else None,
                'publish_days': task.publish_days, 'rows_per_page': 30}
            try:
                operation = None
                if phase in {'submitted', 'unknown'}:
                    try: operation = await dispatcher.get_operation(task.owner_id, ctx['account_id'], ctx['request_id'])
                    except DispatchError as exc:
                        if exc.code != 'operation_not_found': raise
                if operation is None:
                    await svc.mark_page(ctx, 'submitted')
                    async with sessions() as db:
                        operation = await dispatcher.submit(owner_id=task.owner_id, account_id=ctx['account_id'],
                            request_id=ctx['request_id'], command='monitor_search_page', payload=payload, session=db)
                if operation.status == 'confirmed':
                    result = await svc.accept(ctx, operation.result)
                    if result.status in ERRORS or result.status == 'stale_response': return result
                elif operation.status == 'failed':
                    await svc.mark_page(ctx, 'dispatch_failed')
                    return ScanResult('dispatch_failed')
                else:
                    await svc.mark_page(ctx, 'unknown' if operation.status == 'unknown' else 'submitted')
                    return ScanResult(operation.status)
            except DispatchError:
                await svc.mark_page(ctx, 'dispatch_failed')
                return ScanResult('dispatch_failed')
            except ValueError:
                # Missing command registration is an explicit integration state, not a fallback search.
                await svc.mark_page(ctx, 'dispatch_failed')
                return ScanResult('dispatch_unavailable')
        await svc.enqueue_notifications(task_id)
        return result
    finally:
        await svc.release(task_id, token)
