"""验证后才选择淘汰项；日7份、周4份、最近恢复点和显式保护点分别保留。"""
import hashlib
from sqlalchemy import select
from common.models.db_backup_log import DbBackupLog
from common.models.admin_control import BackupVerification
from common.utils.backup_paths import resolve_backup_file


def retention_candidates(logs,checks):
    logs=sorted([l for l in logs if l.status=='success'],key=lambda l:l.created_at,reverse=True)
    if not logs:return []
    restored={c.backup_log_id for c in checks if c.status=='restored'}
    if logs[0].id not in restored:return []
    keep={c.backup_log_id for c in checks if c.protected}
    daily=set();weekly=set()
    for log in logs:
        day=log.created_at.date();week=log.created_at.isocalendar()[:2]
        if day not in daily and len(daily)<7:daily.add(day);keep.add(log.id)
        if week not in weekly and len(weekly)<4:weekly.add(week);keep.add(log.id)
    latest=next((l.id for l in logs if l.id in restored),None)
    keep.add(latest)
    return [l.id for l in logs if l.id not in keep]


async def prune_verified_backups(session, *, apply=False):
    logs=(await session.execute(select(DbBackupLog).order_by(DbBackupLog.created_at.desc()).with_for_update().execution_options(populate_existing=True))).scalars().all()
    checks=(await session.execute(select(BackupVerification).with_for_update().execution_options(populate_existing=True))).scalars().all()
    ids=set(retention_candidates(logs,checks))
    if not ids:return {'eligible':[],'removed':[],'status':'retained'}
    latest=next(l for l in logs if l.status=='success')
    file=resolve_backup_file(latest.file_name)
    if not file:return {'eligible':[],'removed':[],'status':'latest_missing'}
    digest=hashlib.sha256(file.read_bytes()).hexdigest()
    if not any(c.backup_log_id==latest.id and c.status=='restored' and c.sha256==digest for c in checks):
        return {'eligible':[],'removed':[],'status':'checksum_changed'}
    removed=[]
    if apply:
        for log in logs:
            if log.id not in ids:continue
            path=resolve_backup_file(log.file_name)
            if path:path.unlink();removed.append(log.id)
        # 记录保留：后台显示文件已淘汰；审计与恢复验证证据不随文件删除。
    return {'eligible':sorted(ids),'removed':removed,'status':'applied' if apply else 'preview'}
