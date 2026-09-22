"""手动 Cookie 导入后台链：检查→行锁重验任务→凭据写回→按启用状态重启。"""
import asyncio
from sqlalchemy import select
from common.db.session import async_session_maker
from common.models.xy_account import XYAccount
from common.services import account_policy as policy
from common.services.account_credentials import validate_credentials
from app.services.account_service import AccountService


async def restart_account(account_id):
    from app.services.websocket_client import websocket_client
    await websocket_client.restart_account(account_id)


async def run_cookie_import(account_id, owner_id, job_id, cookie):
    try:
        async with async_session_maker() as db:
            svc = AccountService(db)
            a = await svc.get_account_for_user(owner_id, account_id)
            if not a:
                return
            job = await svc.get_credential_job(a, job_id, owner_id)
            if job['status'] in policy.TERMINAL:
                return
            proxy = {k: getattr(a, k) for k in ('proxy_type','proxy_host','proxy_port','proxy_user','proxy_pass')}
            identity = a.unb
        # 不持数据库锁等待平台；取消可立即落库。
        verified = await asyncio.wait_for(validate_credentials(cookie, identity, proxy), timeout=60)
        async with async_session_maker() as db:
            svc = AccountService(db)
            a = await svc.get_account_for_user(owner_id, account_id)
            if not a:
                return
            accepted = await svc.finish_credential_job(a, job_id, verified)
            enabled = a.status == 'active'
        if accepted and enabled:
            try:
                await restart_account(account_id)
            except Exception:
                # 凭据检查成功独立于消费者重启，页面仍显示 unchecked。
                pass
    except Exception:
        async with async_session_maker() as db:
            a = (await db.execute(select(XYAccount).where(
                XYAccount.account_id == account_id, XYAccount.owner_id == owner_id
            ).with_for_update())).scalar_one_or_none()
            if not a:
                return
            policy.fail_job(a, job_id)
            await db.commit()
