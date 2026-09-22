"""Read-only projection for the existing product monitor page."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from common.services import listing_monitor_reliability as monitor
from common.models.notification_delivery import NotificationDelivery


async def reliability_status(session, task_id):
    enabled = monitor.monitor_v2_enabled()
    # Gate closed: do not depend on candidate tables or change the legacy path.
    if not enabled:
        return dict(enabled=False, release_gate='DEV43', phase='disabled', baseline_ready=False,
                    region='', generation=0, pages=[], events=[], items=[])
    svc = monitor.MonitorReliabilityService(async_sessionmaker(session.bind, expire_on_commit=False))
    data = await svc.status(task_id)
    event_ids = [e['notification_event_id'] for e in data['events'] if e['notification_event_id']]
    deliveries = []
    if event_ids:
        deliveries = (await session.execute(select(NotificationDelivery).where(
            NotificationDelivery.event_id.in_(event_ids)))).scalars().all()
    for event in data['events']:
        event['deliveries'] = [dict(channel_id=d.channel_id, status=d.status, attempts=d.attempts)
                              for d in deliveries if d.event_id == event['notification_event_id']]
    return {**data, 'enabled': True, 'release_gate': 'DEV43'}
