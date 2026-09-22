"""白名单数据管理：默认只读；通知配置可预览后原子删除。业务防重事实不清表。"""
import json
import time
from hashlib import sha256
from sqlalchemy import select, func, delete
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from common.models.admin_control import AdminAudit, DataPreview, BackupVerification
from common.models.message_notification import MessageNotification
from common.utils.logging_utils import redact_secrets

class AdminDataService:
    WRITABLE = {'xy_notification_channels', 'xy_message_notifications'}
    # 通用数据工具不暴露任意JSON与自由文本；有权限的专用页面保持原流程。
    HIDDEN = {'password_hash','cookie','login_password','secret_key','proxy_pass','config','metadata','delivery_content','receiver_phone','receiver_address','dock_code'}

    def __init__(self, session, tables, aliases):
        self.session, self.tables, self.aliases = session, tables, aliases

    def table(self, name):
        name = self.aliases.get(name.lower(),name.lower())
        table = self.tables.get(name)
        if table is None:
            raise HTTPException(404, '该表未开放数据操作')
        return table

    async def read(self, name, limit=100, offset=0, owner_id=None):
        table=self.table(name)
        cols=[c for c in table.columns if c.name not in self.HIDDEN]
        stmt=select(*cols); count=select(func.count()).select_from(table)
        if owner_id is not None:
            col=table.c.get('owner_id')
            if col is None:
                raise HTTPException(422,'该表没有归属筛选字段')
            stmt=stmt.where(col==owner_id); count=count.where(col==owner_id)
        rows=(await self.session.execute(stmt.order_by(table.c.id).limit(limit).offset(offset))).mappings().all()
        return dict(success=True,data=redact_secrets(jsonable_encoder(rows)),columns=[c.name for c in cols],count=(await self.session.execute(count)).scalar_one(),limit=limit,offset=offset,capabilities={'read':True,'export':'redacted','delete':table.name in self.WRITABLE})

    async def _impact(self, table, record_id):
        stmt=select(table).order_by(table.c.id).limit(10001).with_for_update()
        if record_id is not None:
            stmt=stmt.where(table.c.id==record_id)
        rows=(await self.session.execute(stmt)).mappings().all()
        if len(rows)>10000:
            raise HTTPException(409,'批次超限，请按单行分批处理')
        related=[]
        if table.name=='xy_notification_channels' and rows:
            related=(await self.session.execute(select(MessageNotification.__table__).where(MessageNotification.channel_id.in_([r['id'] for r in rows])).with_for_update())).mappings().all()
        digest=sha256(json.dumps(jsonable_encoder([rows,related]),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return rows,related,digest

    async def preview(self,name,actor_id,record_id=None):
        table=self.table(name)
        if table.name not in self.WRITABLE:
            raise HTTPException(403,'此表仅开放脱敏查看与导出，业务数据请使用专用流程')
        rows,related,digest=await self._impact(table,record_id)
        preview=DataPreview(actor_id=actor_id,table_name=name,record_id=record_id,digest=digest,expires_at=time.time()+300)
        self.session.add(preview); await self.session.commit()
        return dict(success=True,preview_id=preview.id,count=len(rows),related_count=len(related),owners=sorted({r['owner_id'] for r in rows}),requires_verified_backup=True,expires_in=300)

    async def remove(self,name,actor_id,preview_id,confirmation,record_id=None):
        table=self.table(name)
        if table.name not in self.WRITABLE:
            raise HTTPException(403,'此表仅开放查看')
        try:
            p=(await self.session.execute(select(DataPreview).where(DataPreview.id==preview_id).with_for_update())).scalar_one_or_none()
            if not p or p.actor_id!=actor_id or p.table_name!=name or p.record_id!=record_id or p.consumed or p.expires_at<time.time() or confirmation!='DELETE':
                raise HTTPException(409,'请重新预览并明确确认')
            rows,related,digest=await self._impact(table,record_id)
            if digest!=p.digest:
                raise HTTPException(409,'数据已变化，请重新预览')
            from common.services.data_snapshot import data_fingerprint
            from common.models.db_backup_log import DbBackupLog
            from common.utils.backup_paths import resolve_backup_file
            required_tables = [table]
            if related:
                required_tables.append(MessageNotification.__table__)
            fingerprints = {}
            for required in required_tables:
                contents = (await self.session.execute(select(required).order_by(required.c.id)
                    .limit(10001).with_for_update())).mappings().all()
                if len(contents) > 10000:
                    raise HTTPException(409, '恢复点核对超出在线批次上限')
                fingerprints[required.name] = data_fingerprint(contents)
            candidates = (await self.session.scalars(select(BackupVerification).where(
                BackupVerification.status == 'restored').order_by(BackupVerification.created_at.desc()).limit(20))).all()
            backup = None
            for candidate in candidates:
                report = candidate.report or {}
                if not report.get('isolated') or any(report.get('table_fingerprints', {}).get(name) != value
                                                     for name, value in fingerprints.items()):
                    continue
                log = await self.session.get(DbBackupLog, candidate.backup_log_id)
                file = resolve_backup_file(log.file_name) if log and log.status == 'success' else None
                if file and sha256(file.read_bytes()).hexdigest() == candidate.sha256:
                    backup = candidate
                    break
            if backup is None:
                raise HTTPException(409, '需包含当前配置及关联绑定的有效隔离恢复点')
            if related:
                await self.session.execute(delete(MessageNotification).where(MessageNotification.id.in_([r['id'] for r in related])))
            if rows:
                await self.session.execute(delete(table).where(table.c.id.in_([r['id'] for r in rows])))
            p.consumed=True
            self.session.add(AdminAudit(actor_id=actor_id,action='data_delete',target=name,details={'count':len(rows),'related_count':len(related),'preview_id':p.id,'backup_verification_id':backup.id}))
            await self.session.commit()
            return dict(success=True,deleted=len(rows),related_deleted=len(related))
        except Exception:
            await self.session.rollback()
            raise
