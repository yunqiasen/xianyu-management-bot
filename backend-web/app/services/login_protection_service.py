"""数据库行锁保证多个 Web worker 共用用户名/IP防护，不信任客户端转发头。"""
import time
from hashlib import sha256
from sqlalchemy import select
from common.models.admin_control import LoginProtection, LoginProtectionConfig, AdminAudit
from common.schemas.login_protection import LoginProtectionSettings, LoginProtectionUpdate

class LoginProtectionService:
    WINDOW = 900
    THRESHOLDS = {'username': 5, 'ip': 20}

    def __init__(self, session, clock=time.time):
        self.session, self.clock = session, clock

    async def configuration(self):
        row = await self.session.scalar(select(LoginProtectionConfig).where(
            LoginProtectionConfig.id == 'global').execution_options(populate_existing=True))
        values = LoginProtectionSettings.model_validate(row.values if row else {}).model_dump()
        return {**values, 'version': row.version if row else 0}

    async def configure(self, settings: LoginProtectionUpdate, actor_id: int):
        values = {'id': 'global', 'version': 0, 'values': {}}
        if self.session.bind.dialect.name == 'mysql':
            from sqlalchemy.dialects.mysql import insert
            statement = insert(LoginProtectionConfig).values(**values).on_duplicate_key_update(id='global')
        else:
            from sqlalchemy.dialects.sqlite import insert
            statement = insert(LoginProtectionConfig).values(**values).on_conflict_do_nothing(index_elements=['id'])
        await self.session.execute(statement)
        row = await self.session.scalar(select(LoginProtectionConfig).where(
            LoginProtectionConfig.id == 'global').with_for_update().execution_options(populate_existing=True))
        if row.version != settings.expected_version:
            await self.session.rollback()
            raise ValueError('stale_login_protection_config')
        row.values = settings.model_dump(exclude={'expected_version'})
        row.version += 1
        self.session.add(AdminAudit(actor_id=actor_id, action='login_protection_config',
            target='global', details={'version': row.version, 'fields': sorted(row.values)}))
        await self.session.commit()
        return await self.configuration()

    async def _row(self, dimension, subject):
        if dimension not in self.THRESHOLDS:
            raise ValueError('防护项应为username或ip')
        subject = subject.strip().casefold()[:255]
        key = sha256(f'{dimension}:{subject}'.encode()).hexdigest()
        values = dict(key=key, dimension=dimension, subject=subject, failures=[], locked_until=0)
        if self.session.bind.dialect.name == 'mysql':
            from sqlalchemy.dialects.mysql import insert
            stmt = insert(LoginProtection).values(**values).on_duplicate_key_update(key=key)
        else:
            from sqlalchemy.dialects.sqlite import insert
            stmt = insert(LoginProtection).values(**values).on_conflict_do_nothing(index_elements=['key'])
        await self.session.execute(stmt)
        return (await self.session.execute(select(LoginProtection).where(LoginProtection.key == key).with_for_update().execution_options(populate_existing=True))).scalar_one()

    async def check(self, username, ip=None):
        now = self.clock()
        rows = [await self._row('username', username)]
        if ip:
            rows.append(await self._row('ip', ip))
        return any(row.locked_until > now for row in rows)

    async def record(self, username, ip=None, *, success=False):
        now = self.clock()
        config = await self.configuration()
        for dimension, subject in [('username', username), ('ip', ip)]:
            if not subject:
                continue
            row = await self._row(dimension, subject)
            if success:
                # 一个用户成功不抹除这个IP对其他用户的攻击记录。
                if dimension == 'username':
                    row.failures, row.locked_until = [], 0
                continue
            failures = [t for t in row.failures if t > now - config['window_seconds']]
            failures.append(now)
            row.failures = failures
            if len(failures) >= config[dimension + '_threshold']:
                row.locked_until = now + config['lock_seconds']
        await self.session.commit()

    async def unlock(self, dimension, subject, actor_id):
        row = await self._row(dimension, subject)
        row.failures, row.locked_until = [], 0
        self.session.add(AdminAudit(actor_id=actor_id, action='login_unlock', target=f'{dimension}:{row.subject}', details={}))
        await self.session.commit()

    async def list(self, limit=100, offset=0):
        config = await self.configuration()
        rows = (await self.session.execute(select(LoginProtection).order_by(LoginProtection.locked_until.desc(), LoginProtection.key).limit(limit).offset(offset))).scalars()
        now = self.clock()
        return [dict(dimension=r.dimension, subject=r.subject, failures=sum(t > now-config['window_seconds'] for t in r.failures), locked_until=r.locked_until, locked=r.locked_until > now) for r in rows]
