"""Chromium -> real admin APIs -> SQL and a fresh, isolated MySQL restore schema."""
import asyncio
from contextlib import asynccontextmanager
import gzip
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
from unittest.mock import patch

from support import Base, User, UserRole, XYAccount, NotificationChannel, MessageNotification
from common.models.admin_control import AdminAudit, BackupVerification, DataPreview, LoginProtection, LoginProtectionConfig, AdminAuditArchive
from common.models.db_backup_log import DbBackupLog
from app.api import deps
from app.api.routes import admin, db_backup_logs
from app.services.auth import AuthService
from app.services.login_protection_service import LoginProtectionService
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from playwright.async_api import async_playwright, expect
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import uvicorn


@asynccontextmanager
async def restore_fixture():
    container = 'xymb-integration-mysql-restore-1'
    label = subprocess.check_output(['docker', 'inspect', '--format',
        '{{ index .Config.Labels "com.docker.compose.project" }}', container], text=True).strip()
    if label != 'xymb-integration':
        raise ValueError('isolated_restore_fixture_required')
    suffix = secrets.token_hex(8)
    database, user, password = 'xymb_restore_browser_' + suffix, 'browser_' + suffix, secrets.token_hex(24)
    def sql(statement):
        result = subprocess.run(['docker', 'exec', '-i', container, 'sh', '-c',
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=socket -uroot --batch'],
            input=statement, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError('isolated_restore_fixture_admin_failed')
    engine = None
    try:
        sql(f"CREATE DATABASE `{database}`; CREATE USER '{user}'@'%' IDENTIFIED BY '{password}'; GRANT ALL ON `{database}`.* TO '{user}'@'%';")
        engine = create_async_engine(URL.create('mysql+asyncmy', username=user, password=password,
            host='127.0.0.1', port=19007, database=database), echo=False, hide_parameters=True)
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
        sql(f"DROP DATABASE IF EXISTS `{database}`; DROP USER IF EXISTS '{user}'@'%';")


async def main():
    with tempfile.TemporaryDirectory(prefix='xymb-admin-browser-') as temp:
        root = Path(temp)
        engine = create_async_engine('sqlite+aiosqlite:///' + str(root/'ui.sqlite'))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.exec_driver_sql('PRAGMA journal_mode=WAL')
            for model in (User, XYAccount, NotificationChannel, MessageNotification, LoginProtection,
                          LoginProtectionConfig, AdminAudit, AdminAuditArchive, BackupVerification, DataPreview, DbBackupLog):
                await connection.run_sync(model.__table__.create)
        backup = root/'sample.sql.gz'
        async with sessions() as session:
            user = User(id=1, username='seller', email='fixture@example.test', password_hash='fixture', role=UserRole.ADMIN)
            session.add_all([user, XYAccount(id=1, owner_id=1, account_id='fixture', cookie='PRIVATE_COOKIE', login_method='manual'),
                NotificationChannel(id=11, owner_id=1, name='Fixture channel', channel_type='webhook', config_payload={'token':'PRIVATE_NOTIFY'}),
                MessageNotification(id=11, owner_id=1, account_pk=1, account_identifier='fixture', channel_id=11),
                DbBackupLog(id=1, status='success', file_name=backup.name, table_count=2, total_rows=2)])
            await session.commit()
            token = AuthService(session).create_access_token(user)
            guard = LoginProtectionService(session)
            for _ in range(20):
                await guard.record('seller', '192.0.2.10')
            from common.services.audit_retention import archive_audit_history
            from datetime import timedelta
            from common.utils.time_utils import get_beijing_now_naive
            session.add(AdminAudit(id='archive-ui-fixture', actor_id=1, action='user_update', target='2',
                details={'fields': ['role'], 'token': 'PRIVATE_AUDIT'},
                created_at=get_beijing_now_naive()-timedelta(days=181)))
            await session.commit()
            await archive_audit_history(session)
        from datetime import datetime
        import json
        import re
        from sqlalchemy import select
        from sqlalchemy.schema import CreateTable
        from sqlalchemy.dialects import mysql
        def literal(value):
            if value is None: return 'NULL'
            if isinstance(value, bool): return '1' if value else '0'
            if isinstance(value, (int, float)): return str(value)
            if isinstance(value, datetime): value = value.strftime('%Y-%m-%d %H:%M:%S')
            if isinstance(value, dict): value = json.dumps(value)
            return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
        async with sessions() as session:
            with gzip.open(backup, 'wt') as stream:
                stream.write('SET FOREIGN_KEY_CHECKS=0;\n')
                for model in (NotificationChannel, MessageNotification):
                    table = model.__table__
                    ddl = str(CreateTable(table).compile(dialect=mysql.dialect())).strip()
                    ddl = re.sub(r'CREATE TABLE (\w+)', r'CREATE TABLE `\1`', ddl, count=1)
                    if model is MessageNotification:
                        end = ddl.rfind(')')
                        ddl = ddl[:end].rstrip() + ', CONSTRAINT `fixture_channel_fk` FOREIGN KEY (`channel_id`) REFERENCES `xy_notification_channels` (`id`)\n' + ddl[end:]
                    stream.write(ddl + ';\n')
                    for row in (await session.execute(select(table))).mappings():
                        columns = ','.join('`' + key + '`' for key in row)
                        values = ','.join(literal(value) for value in row.values())
                        stream.write(f'INSERT INTO `{table.name}` ({columns}) VALUES ({values});\n')
                stream.write('SET FOREIGN_KEY_CHECKS=1;\n')
        bundle = root/'app.js'
        subprocess.run(['node', 'tests/admin/browser-build.cjs', str(bundle)], check=True)
        app = FastAPI()
        app.include_router(admin.router, prefix='/api/v1/admin')
        app.include_router(db_backup_logs.router, prefix='/api/v1')
        async def db():
            async with sessions() as session:
                yield session
        app.dependency_overrides[deps.get_db_session] = db
        @app.get('/')
        async def index():
            return HTMLResponse('<html><body><div id="root"></div><script src="/app.js"></script></body></html>')
        @app.get('/app.js')
        async def script():
            return FileResponse(bundle, media_type='text/javascript')
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                if task.done(): await task
                await asyncio.sleep(.01)
            url = f'http://127.0.0.1:{port}'
            with patch('common.utils.backup_paths.get_backup_root', return_value=root):
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(executable_path=str(sorted((Path.home() / '.cache/ms-playwright').glob('chromium-*/chrome-*/chrome'))[-1]), headless=True, args=['--no-sandbox'])
                    context = await browser.new_context(extra_http_headers={'Authorization':'Bearer ' + token})
                    page = await context.new_page(); page.set_default_timeout(5000)
                    errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(url + '/') else route.abort())
                    await page.goto(url)
                    guard_ui = page.get_by_role('region', name='后台登录防护')
                    username = guard_ui.get_by_role('row').filter(has_text='seller')
                    ip = guard_ui.get_by_role('row').filter(has_text='192.0.2.10')
                    await expect(username).to_contain_text('锁定至')
                    await username.get_by_role('button', name='定向解锁').click()
                    await expect(username).to_contain_text('未锁定')
                    await expect(ip).to_contain_text('锁定至')
                    await guard_ui.get_by_label('用户名失败阈值').fill('3')
                    await guard_ui.get_by_label('IP失败阈值').fill('30')
                    await guard_ui.get_by_role('button', name='保存防护配置').click()
                    await guard_ui.get_by_text('配置已保存', exact=False).wait_for()
                    config = (await (await page.request.get(url + '/api/v1/admin/login-protection/config')).json())['data']
                    assert config['username_threshold'] == 3 and config['ip_threshold'] == 30 and config['version'] == 1, config
                    data_ui = page.get_by_role('region', name='数据管理')
                    await expect(data_ui.get_by_role('button', name='预览清表影响')).to_be_disabled()
                    assert 'PRIVATE_COOKIE' not in await data_ui.inner_text()
                    await data_ui.get_by_label('数据表').select_option('xy_notification_channels')
                    await expect(data_ui.get_by_text('Fixture channel', exact=True)).to_be_visible()
                    assert 'PRIVATE_NOTIFY' not in await data_ui.inner_text()
                    await data_ui.get_by_role('button', name='预览清表影响').click()
                    await data_ui.get_by_text('本表 1 条；关联绑定 1 条；所属用户 1。', exact=True).wait_for()
                    await data_ui.get_by_label('输入DELETE确认').fill('DELETE')
                    await data_ui.get_by_role('button', name='确认原子删除').click()
                    await data_ui.get_by_text('删除未执行', exact=False).wait_for()
                    backup_ui = page.get_by_role('region', name='备份验证')
                    await backup_ui.get_by_text('校验 / 隔离恢复', exact=True).click()
                    await backup_ui.get_by_role('button', name='校验SQL.gz').click()
                    await backup_ui.get_by_text('文件校验通过，尚未完成恢复', exact=True).wait_for()
                    await backup_ui.get_by_role('button', name='设为保护点', exact=True).click()
                    await backup_ui.get_by_text('已保护，自动清理会保留这份备份', exact=True).wait_for()
                    checks = (await (await page.request.get(url + '/api/v1/db-backup-logs/1/verifications')).json())['data']
                    assert checks[0]['protected'] is True, checks
                    await backup_ui.get_by_role('button', name='取消保护', exact=True).click()
                    await backup_ui.get_by_text('已取消保护，后续按保留策略处理', exact=True).wait_for()
                    await backup_ui.get_by_role('button', name='设为保护点', exact=True).click()
                    await backup_ui.get_by_text('已保护，自动清理会保留这份备份', exact=True).wait_for()
                    # A real fresh schema, not a seeded "restored" record, opens the delete gate.
                    async with restore_fixture() as restore:
                        app.state.admin_restore_engine = restore
                        await backup_ui.get_by_role('button', name='隔离库恢复验证').click()
                        await backup_ui.get_by_text('隔离恢复验证通过', exact=True).wait_for()
                        checks = (await (await page.request.get(url + '/api/v1/db-backup-logs/1/verifications')).json())['data']
                        restored = next(check for check in checks if check['status'] == 'restored')
                        assert restored['report']['foreign_keys_checked'] == 1, checks
                        await data_ui.get_by_role('button', name='确认原子删除').click()
                        await data_ui.get_by_text('已删除并记录审计', exact=True).wait_for()
                        await data_ui.get_by_label('数据表').select_option('xy_message_notifications')
                        await data_ui.get_by_text('共 0 条', exact=False).wait_for()
                        app.state.admin_restore_engine = None
                    archive_ui = page.get_by_role('region', name='日志归档')
                    await archive_ui.get_by_text('结构化日志归档 · 待核实记录保留在线', exact=True).click()
                    await archive_ui.get_by_label('归档类别').select_option('audit')
                    await archive_ui.get_by_role('button', name='查询归档', exact=True).click()
                    await archive_ui.get_by_text('xy_admin_audit #archive-ui-fixture', exact=True).click()
                    await expect(archive_ui).to_contain_text('user_update')
                    assert 'PRIVATE_AUDIT' not in await archive_ui.inner_text()
                    assert not errors, errors
                    await page.screenshot(path=str(Path(os.environ.get('XYMB_VERIFY_OUTPUT', temp))/'admin-management.png'), full_page=True)
                    await browser.close()
            print('PASS Chromium admin config, targeted unlock, readonly data, protected backup, deletion preview and genuine isolated MySQL restore before atomic deletion')
        finally:
            server.should_exit = True
            await task
            await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
