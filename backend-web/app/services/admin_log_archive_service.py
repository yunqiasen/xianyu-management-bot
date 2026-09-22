from types import SimpleNamespace
from datetime import timedelta
from sqlalchemy import select,delete
from fastapi.encoders import jsonable_encoder
from common.models.admin_control import AdminLogArchive
from common.utils.logging_utils import redact_secrets
from common.utils.time_utils import get_beijing_now_naive

TERMINAL=('success','failed','cancelled','skipped','skipped_cooldown','no_credentials','token_refreshed')

async def archive_log_delete(session,statement):
    """与调用方同一事务；只归档30天前的明确终态，保留所有未知/处理中记录。"""
    table=statement.table
    if not (table.name.endswith('_logs') or table.name.endswith('_log')):
        raise ValueError('此入口仅处理运行日志')
    state=next((table.c[k] for k in ('login_status','processing_status','status') if k in table.c),None)
    if state is None:return SimpleNamespace(rowcount=0)
    query=select(*table.c).where(table.c.created_at < get_beijing_now_naive()-timedelta(days=30),state.in_(TERMINAL)).limit(1000).with_for_update()
    if statement.whereclause is not None:query=query.where(statement.whereclause)
    rows=(await session.execute(query)).mappings().all()
    for row in rows:
        existing=(await session.execute(select(AdminLogArchive.id).where(AdminLogArchive.source_table==table.name,AdminLogArchive.source_id==row['id']))).scalar_one_or_none()
        if not existing:session.add(AdminLogArchive(source_table=table.name,source_id=row['id'],payload=redact_secrets(jsonable_encoder(dict(row)))))
    await session.flush()
    if rows:await session.execute(delete(table).where(table.c.id.in_([r['id'] for r in rows])))
    return SimpleNamespace(rowcount=len(rows))
