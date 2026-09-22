"""仅接受现有备份记录；解析、校验与独立MySQL恢复分别报告。"""
import gzip
from contextlib import asynccontextmanager
import hashlib
import re
from sqlalchemy import text,select
from common.models.admin_control import BackupVerification,AdminAudit
from common.models.db_backup_log import DbBackupLog
from common.utils.backup_paths import resolve_backup_file


def _statements(sql):
    sql=re.sub(r'^\s*--[^\n]*','',sql,flags=re.M)
    buf=[];quote=None;escape=False
    for ch in sql:
        buf.append(ch)
        if escape: escape=False;continue
        if quote:
            if ch=='\\':escape=True
            elif ch==quote:quote=None
        elif ch in "'\"`":quote=ch
        elif ch==';':
            yield ''.join(buf).strip();buf=[]
    if quote or ''.join(buf).strip():raise ValueError('SQL内容截断或缺少分号')


def inspect_backup(path):
    try:
        with gzip.open(path,'rt',encoding='utf-8') as stream: sql=stream.read(128*1024*1024+1)
    except (OSError,EOFError,UnicodeError):raise ValueError('备份损坏或不是SQL.gz') from None
    if len(sql)>128*1024*1024:raise ValueError('备份超过在线校验上限，需分批恢复')
    statements=list(_statements(sql)); tables=[]; rows={}
    for stmt in statements:
        if re.fullmatch(r"SET\s+(NAMES\s+utf8mb4|FOREIGN_KEY_CHECKS\s*=\s*[01]|SQL_MODE\s*=\s*'NO_AUTO_VALUE_ON_ZERO'|TIME_ZONE\s*=\s*'\+00:00')\s*;",stmt,re.I):continue
        match=re.match(r'(CREATE TABLE|DROP TABLE IF EXISTS|INSERT INTO)\s+`([A-Za-z0-9_]+)`\s*(.*)',stmt,re.I|re.S)
        if not match:raise ValueError('备份含白名单外SQL语句')
        action,name,tail=match.groups();action=action.upper()
        if action=='CREATE TABLE':
            if name in tables or not tail.startswith('('):raise ValueError('表结构重复或异常')
            # 独立库只接受常规表结构，屏蔽外部目录/跨库/执行注释。
            stripped=re.sub(r"'(?:\\.|[^'])*'", "''", tail)
            if re.search(r'/\*|\b(DATA DIRECTORY|INDEX DIRECTORY|TABLESPACE|SELECT|UNION)\b|`\s*\.',stripped,re.I):raise ValueError('表定义超出恢复白名单')
            tables.append(name);rows.setdefault(name,0)
        elif action=='INSERT INTO':
            if name not in tables or not re.match(r'\([^;]+?\)\s+VALUES\s*\(',tail,re.I|re.S):raise ValueError('数据缺少对应表结构')
            rows[name]+=1  # 原备份器每条语句恰好一行
        elif tail.strip()!=';':raise ValueError('DROP语句格式有误')
    if not tables:raise ValueError('备份没有表结构')
    return {'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'tables':tables,'rows':rows,'statements':statements}


@asynccontextmanager
async def _restore_session_settings(connection):
    original = (await connection.execute(text(
        'SELECT @@SESSION.sql_mode,@@SESSION.time_zone,@@SESSION.foreign_key_checks'))).one()
    try:
        yield
    finally:
        # Restore pooled-connection settings even when an import statement fails.
        for name, value in zip(('sql_mode', 'time_zone', 'foreign_key_checks'), original):
            await connection.execute(text(f'SET SESSION {name}=:value'), {'value': value})


class BackupProtectionError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def verification_view(record):
    return {'id': record.id, 'status': record.status, 'sha256': record.sha256,
            'report': record.report, 'protected': record.protected}


class BackupVerificationService:
    def __init__(self,session):self.session=session
    async def set_protection(self, log_id, verification_id, actor_id, protected):
        # Lock the parent in the same order as retention, so a completed protection
        # request is never followed by deletion from a stale retention snapshot.
        log = await self.session.scalar(select(DbBackupLog).where(DbBackupLog.id == log_id)
            .with_for_update().execution_options(populate_existing=True))
        record = await self.session.scalar(select(BackupVerification).where(
            BackupVerification.id == verification_id, BackupVerification.backup_log_id == log_id)
            .with_for_update().execution_options(populate_existing=True))
        if log is None or record is None:
            raise BackupProtectionError('备份验证记录不存在', 404)
        if protected:
            path = resolve_backup_file(log.file_name)
            if log.status != 'success' or record.status not in {'checked', 'restored'} or not path:
                raise BackupProtectionError('请选择文件仍在的已验证备份')
            if hashlib.sha256(path.read_bytes()).hexdigest() != record.sha256:
                raise BackupProtectionError('备份文件已变化，请重新验证')
        if record.protected != protected:
            record.protected = protected
            self.session.add(AdminAudit(actor_id=actor_id, action='backup_protection', target=str(log_id),
                details={'verification_id': record.id, 'protected': protected}))
        await self.session.commit()
        return verification_view(record)

    async def verify(self,log_id,actor_id,restore_engine=None):
        log=await self.session.get(DbBackupLog,log_id)
        if not log or log.status!='success':raise ValueError('请选择成功的备份记录')
        path=resolve_backup_file(log.file_name)
        if not path:raise ValueError('备份文件不在当前备份目录')
        info=inspect_backup(path)
        if log.table_count is not None and log.table_count!=len(info['tables']):raise ValueError('表数量与备份记录不一致')
        if log.total_rows is not None and log.total_rows!=sum(info['rows'].values()):raise ValueError('行数量与备份记录不一致')
        record=BackupVerification(backup_log_id=log_id,sha256=info['sha256'],status='checked',report={k:v for k,v in info.items() if k!='statements'})
        self.session.add(record);await self.session.commit()
        if restore_engine is not None:
            try:
                record.status='restoring';await self.session.commit()
                report=await self.restore(info,restore_engine)
                record.status='restored';record.report={**record.report,**report}
            except Exception as exc:
                record.status='failed';record.report={**record.report,'error_type':type(exc).__name__}
            self.session.add(AdminAudit(actor_id=actor_id,action='backup_restore_verify',target=str(log_id),details={'status':record.status,'verification_id':record.id}))
            await self.session.commit()
        return verification_view(record)
    async def restore(self,info,engine):
        source=self.session.bind.url;target=engine.url
        if target.get_backend_name()!='mysql' or not (target.database or '').startswith('xymb_restore_'):
            raise ValueError('恢复目标须为独立MySQL的xymb_restore_前缀空库')
        if (source.host,source.port or 3306)==(target.host,target.port or 3306):raise ValueError('恢复必须使用不同MySQL实例')
        async with engine.begin() as conn, _restore_session_settings(conn):
            if source.get_backend_name() == 'mysql':
                source_uuid=(await self.session.execute(text('SELECT @@server_uuid'))).scalar_one()
                target_uuid=(await conn.exec_driver_sql('SELECT @@server_uuid')).scalar_one()
                if source_uuid==target_uuid:raise ValueError('恢复目标与现用实例相同')
            existing=(await conn.exec_driver_sql('SHOW TABLES')).all()
            if existing:raise ValueError('隔离库须为空库')
            for stmt in info['statements']:await conn.exec_driver_sql(stmt)
            counts={}
            for name,expected in info['rows'].items():
                counts[name]=(await conn.exec_driver_sql(f'SELECT COUNT(*) FROM `{name}`')).scalar_one()
                if counts[name]!=expected:raise ValueError('恢复行数不一致')
            # MySQL重新开启FK检查不会回查已有行，因此显式检查每一组约束（支持复合键）。
            fks=(await conn.execute(text('SELECT TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION'))).all()
            grouped={}
            for t,k,c,rt,rc in fks:grouped.setdefault((t,k,rt),[]).append((c,rc))
            for (t,k,rt),pairs in grouped.items():
                names=[t,rt]+[v for pair in pairs for v in pair]
                if not all(re.fullmatch('[A-Za-z0-9_]+',v) for v in names):raise ValueError('约束字段超出标识符范围')
                on=' AND '.join(f'a.`{c}`=b.`{rc}`' for c,rc in pairs)
                where=' AND '.join(f'a.`{c}` IS NOT NULL' for c,_ in pairs)
                missing=(await conn.exec_driver_sql(f'SELECT COUNT(*) FROM `{t}` a LEFT JOIN `{rt}` b ON {on} WHERE {where} AND b.`{pairs[0][1]}` IS NULL')).scalar_one()
                if missing:raise ValueError('恢复存在外键孤儿记录')
            for child,field,parent in [('xy_accounts','owner_id','xy_users'),('xy_notification_channels','owner_id','xy_users'),('xy_message_notifications','channel_id','xy_notification_channels'),('xy_message_notifications','account_pk','xy_accounts')]:
                if child in counts and parent in counts:
                    missing=(await conn.exec_driver_sql(f'SELECT COUNT(*) FROM `{child}` a LEFT JOIN `{parent}` b ON a.`{field}`=b.id WHERE b.id IS NULL')).scalar_one()
                    if missing:raise ValueError('恢复存在业务关联孤儿记录')
            # A restorable file is only a deletion recovery point when it contains
            # the same configuration rows. Record hashes, never copied secrets.
            from sqlalchemy import inspect
            from common.models.notification_channel import NotificationChannel
            from common.models.message_notification import MessageNotification
            from common.services.data_snapshot import data_fingerprint
            fingerprints = {}
            for model in (NotificationChannel, MessageNotification):
                table = model.__table__
                if table.name not in counts:
                    continue
                columns = await conn.run_sync(lambda c: {v['name'] for v in inspect(c).get_columns(table.name)})
                if columns == set(table.columns.keys()):
                    rows = (await conn.execute(select(table))).mappings().all()
                    fingerprints[table.name] = data_fingerprint(rows)
            return {'restored_rows':counts,'foreign_keys_checked':len(grouped),
                    'table_fingerprints':fingerprints,'isolated':True}
