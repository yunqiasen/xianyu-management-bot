"""Move completed management audits after 180 days; never delete audit evidence."""
from datetime import timedelta
from sqlalchemy import delete, select
from common.models.admin_control import AdminAudit, AdminAuditArchive
from common.utils import time_utils
from common.utils.logging_utils import redact_secrets

COMPLETED_ACTIONS = frozenset({
    'user_update', 'user_disable', 'login_unlock', 'login_protection_config',
    'data_delete', 'backup_protection', 'backup_restore_verify',
    'configuration.account', 'configuration.user', 'configuration.system',
})
COMPLETED_STATES = frozenset({'success', 'failed', 'cancelled', 'skipped', 'checked', 'restored', 'confirmed'})


def _completed(record):
    details = record.details or {}
    if record.action not in COMPLETED_ACTIONS:
        return False
    if any(key in details for key in ('intent_id', 'operation_id', 'request_id')):
        # Future audit producers with business references need an explicit lifecycle
        # adapter before those references can move out of the online table.
        return False
    return all(details[key] in COMPLETED_STATES for key in ('status', 'state') if key in details)


async def archive_audit_history(session, *, limit=1000):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('invalid_archive_batch_size')
    cutoff = time_utils.get_beijing_now_naive() - timedelta(days=180)
    moved, retained, cursor = 0, 0, ''
    # Keyset scanning passes retained pending rows; they never starve later completed
    # audits. Each move and source deletion commit together, including on restart.
    while moved < limit:
        rows = list(await session.scalars(select(AdminAudit).where(
            AdminAudit.created_at < cutoff, AdminAudit.id > cursor,
            AdminAudit.action.in_(COMPLETED_ACTIONS))
            .order_by(AdminAudit.id).limit(limit - moved).with_for_update(skip_locked=True)))
        if not rows:
            break
        ids = []
        for record in rows:
            cursor = record.id
            if not _completed(record):
                retained += 1
                continue
            payload = redact_secrets({
                'id': record.id, 'actor_id': record.actor_id, 'action': record.action,
                'target': record.target, 'details': record.details,
                'created_at': record.created_at.isoformat(), 'updated_at': record.updated_at.isoformat(),
            })
            existing = await session.get(AdminAuditArchive, record.id)
            if existing is not None and existing.payload != payload:
                raise ValueError('audit_archive_conflict')
            if existing is None:
                session.add(AdminAuditArchive(id=record.id, payload=payload))
            ids.append(record.id)
        await session.flush()
        if ids:
            await session.execute(delete(AdminAudit).where(AdminAudit.id.in_(ids)))
            moved += len(ids)
    await session.commit()
    return {'archived': moved, 'retained_pending': retained}
