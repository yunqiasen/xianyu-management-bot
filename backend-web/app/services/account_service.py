"""
账号服务

功能：
1. 账号CRUD操作
2. 账号状态管理
3. Cookie更新
4. 扫码登录账号创建/更新
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.services.account_limit_service import AccountLimitService
from common.services import account_policy
from common.models.xy_account import XYAccount
from common.models.user import User
from common.utils.cookie_refresh import clear_cookie_refresh_snapshot

# UTC时区常量
UTC = timezone.utc


class AccountService:
    """Provides access to legacy cookie account records."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _lock_account(self, account: XYAccount) -> XYAccount:
        result = await self.session.execute(
            select(XYAccount).where(XYAccount.id == account.id)
            .with_for_update().execution_options(populate_existing=True)
        )
        locked = result.scalars().first()
        if locked is None:
            raise ValueError("账号不存在")
        return locked

    async def _check_identity_owner(self, owner_id: int, unb: str | None) -> None:
        if not unb:
            raise ValueError("Cookie 缺少账号身份 unb")
        result = await self.session.execute(select(XYAccount).where(XYAccount.unb == unb))
        if any(row.owner_id != owner_id for row in result.scalars().all()):
            raise ValueError("闲鱼身份已绑定其他后台用户")

    async def start_credential_job(self, account, kind, owner_id):
        account = await self._lock_account(account)
        previous = account_policy.snapshot(account)['jobs']
        job = account_policy.start_job(account, kind=kind, owner_id=owner_id)
        await self.session.commit()
        return {**job, 'created': job['id'] not in previous}

    async def get_credential_job(self, account, job_id, owner_id):
        account = await self._lock_account(account)
        job = account_policy.get_job(account, job_id, owner_id=owner_id)
        await self.session.commit()
        return job

    async def cancel_credential_job(self, account, job_id, owner_id):
        account = await self._lock_account(account)
        job = account_policy.cancel_job(account, job_id, owner_id=owner_id)
        await self.session.commit()
        return job

    async def finish_credential_job(self, account, job_id, cookie):
        account = await self._lock_account(account)
        accepted = account_policy.complete_job(account, job_id, cookie)
        await self.session.commit()
        return accepted

    async def ack_configuration(self, account, consumer, version):
        account = await self._lock_account(account)
        accepted = account_policy.ack_config(account, consumer, version)
        await self.session.commit()
        return accepted

    async def save_request_policy(self, account, payload):
        from common.services.account_configuration import request_policy_view
        account = await self._lock_account(account)
        previous = account_policy.snapshot(account)
        if previous['config_version'] != payload.expected_config_version:
            raise account_policy.StaleAccountOperation('configuration_changed')
        from common.services.typed_settings import clear_inheritance
        clear_inheritance(account, ['risk'])
        values = {**previous.get('config_values', {}), 'risk': payload.risk_values()}
        account_policy.bump_config(account, values)
        if previous['business_state'] in {'paused', 'verification_required', 'cooldown', 'proxy_error', 'recovering'}:
            current = account_policy.snapshot(account)
            current.update(business_state=previous['business_state'], reason=previous['reason'])
            account_policy.store(account, current)
        await self.session.commit()
        return request_policy_view(account)

    async def list_account_options(self, owner_id: int | None = None) -> list[dict]:
        stmt = select(
            XYAccount.id,
            XYAccount.account_id,
            XYAccount.remark,
            XYAccount.status,
            XYAccount.show_browser,
        ).where(XYAccount.status != "deleted").order_by(XYAccount.account_id)
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        return [
            {
                "pk": row.id,
                "id": row.account_id,
                "remark": row.remark or "",
                "enabled": (row.status or "active").strip().lower() not in {"inactive", "disabled", "suspended", "deleted"},
                "show_browser": bool(row.show_browser),
            }
            for row in result.all()
        ]

    async def list_account_ids(self, owner_id: int | None = None) -> list[str]:
        """获取账号ID列表，owner_id为None时返回所有账号（管理员）"""
        stmt = select(XYAccount.account_id).where(XYAccount.status != "deleted").order_by(XYAccount.account_id)
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_accounts(self, owner_id: int | None = None) -> list[XYAccount]:
        """获取账号列表，owner_id为None时返回所有账号（管理员）"""
        stmt = select(XYAccount).where(XYAccount.status != "deleted").order_by(XYAccount.account_id)
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_accounts_paginated(
        self,
        owner_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        ai_reply: bool | None = None,
        scheduled_redelivery: bool | None = None,
        scheduled_rate: bool | None = None,
        auto_polish: bool | None = None,
        auto_confirm: bool | None = None,
        has_password: bool | None = None,
        disable_reason: str | None = None,
        account_id: str | None = None,
        online: bool | None = None,
        online_account_ids: list[str] | None = None,
        owner_username: str | None = None,
    ) -> tuple[list[XYAccount], int]:
        """获取账号列表（分页），支持多条件筛选
        
        Args:
            owner_id: 用户ID，None表示查询所有用户（管理员）
            page: 页码
            page_size: 每页数量
            status: 状态筛选（active/inactive）
            ai_reply: AI回复开关筛选
            scheduled_redelivery: 定时补发货筛选
            scheduled_rate: 定时补评价筛选
            auto_polish: 商品擦亮筛选
            auto_confirm: 自动确认收货筛选
            has_password: 是否配置密码筛选
            disable_reason: 禁用原因模糊搜索关键词（LIKE %keyword%）
            account_id: 账号ID模糊搜索关键词（LIKE %keyword%）
            online: 在线状态筛选（True=仅在线 / False=仅离线 / None=不筛选）
            online_account_ids: 当前在线账号ID集合（口径同仪表盘“在线账号”，由调用方实时取得）

        Returns:
            (账号列表, 总数)
        """
        from sqlalchemy import func, and_, or_
        
        base_stmt = select(XYAccount)
        conditions = [XYAccount.status != "deleted"]
        
        # 用户ID筛选
        if owner_id is not None:
            conditions.append(XYAccount.owner_id == owner_id)
        
        # 状态筛选 - 与 _status_to_enabled 函数保持一致
        # inactive/disabled/suspended/deleted 视为禁用，其他视为启用
        if status is not None:
            inactive_statuses = ["inactive", "disabled", "suspended", "deleted"]
            if status == "active":
                # 启用：status 不在禁用列表中
                conditions.append(~XYAccount.status.in_(inactive_statuses))
            elif status == "inactive":
                # 禁用：status 在禁用列表中
                conditions.append(XYAccount.status.in_(inactive_statuses))
        
        # AI回复筛选（从metadata_json中获取）
        if ai_reply is not None:
            if ai_reply:
                # AI回复开启：兼容 ai_enabled 与历史 enabled 字段
                conditions.append(
                    or_(
                        XYAccount.metadata_json["ai_reply_settings"]["ai_enabled"].as_boolean() == True,
                        XYAccount.metadata_json["ai_reply_settings"]["enabled"].as_boolean() == True,
                    )
                )
            else:
                # AI回复关闭：metadata_json为空，或 ai_enabled/enabled 都未开启
                conditions.append(
                    or_(
                        XYAccount.metadata_json.is_(None),
                        XYAccount.metadata_json["ai_reply_settings"]["ai_enabled"].as_boolean() == False,
                        XYAccount.metadata_json["ai_reply_settings"]["enabled"].as_boolean() == False,
                        and_(
                            XYAccount.metadata_json["ai_reply_settings"]["ai_enabled"].is_(None),
                            XYAccount.metadata_json["ai_reply_settings"]["enabled"].is_(None),
                        )
                    )
                )
        
        # 定时补发货筛选
        if scheduled_redelivery is not None:
            conditions.append(XYAccount.scheduled_redelivery == scheduled_redelivery)
        
        # 定时补评价筛选
        if scheduled_rate is not None:
            conditions.append(XYAccount.scheduled_rate == scheduled_rate)
        
        # 商品擦亮筛选
        if auto_polish is not None:
            conditions.append(XYAccount.auto_polish == auto_polish)
        
        # 自动确认收货筛选
        if auto_confirm is not None:
            conditions.append(XYAccount.auto_confirm == auto_confirm)
        
        # 禁用原因模糊搜索（忽略空白字符串；ilike 大小写不敏感，自动参数化避免 SQL 注入；与项目其它筛选保持风格一致）
        if disable_reason is not None:
            keyword = disable_reason.strip()
            if keyword:
                conditions.append(XYAccount.disable_reason.ilike(f"%{keyword}%"))
        
        # 账号ID模糊搜索（忽略空白字符串；ilike 自动参数化避免 SQL 注入；与禁用原因模糊搜索保持风格一致）
        if account_id is not None:
            account_id_keyword = account_id.strip()
            if account_id_keyword:
                conditions.append(XYAccount.account_id.ilike(f"%{account_id_keyword}%"))

        # 所属用户名模糊搜索（管理员按账号归属用户筛选）：
        # 账号表无用户名字段，故通过子查询匹配 users.username，再以 owner_id IN (...) 过滤。
        # ilike 自动参数化避免 SQL 注入；普通用户已被上方 owner_id 作用域限制，故此条件不影响数据隔离。
        if owner_username is not None:
            owner_username_keyword = owner_username.strip()
            if owner_username_keyword:
                owner_ids_subq = select(User.id).where(
                    User.username.ilike(f"%{owner_username_keyword}%")
                )
                conditions.append(XYAccount.owner_id.in_(owner_ids_subq))

        # 在线状态筛选：在线集合来自 websocket 实时连接（不在库内），
        # 故以 account_id IN / NOT IN 在线集合 的方式参与 SQL 条件，保证分页正确。
        # 空集合时：online=True 匹配为空（无人在线）；online=False 匹配全部（与语义一致）。
        if online is not None:
            online_ids = [str(x) for x in (online_account_ids or [])]
            if online:
                conditions.append(XYAccount.account_id.in_(online_ids))
            else:
                conditions.append(XYAccount.account_id.notin_(online_ids))
        
        # 是否配置密码筛选（账号和密码都配置了才算已配置）
        if has_password is not None:
            if has_password:
                # 已配置：username和login_password都不为空
                conditions.append(
                    and_(
                        XYAccount.username.isnot(None),
                        XYAccount.username != '',
                        XYAccount.login_password.isnot(None),
                        XYAccount.login_password != ''
                    )
                )
            else:
                # 未配置：username或login_password为空
                conditions.append(
                    or_(
                        XYAccount.username.is_(None),
                        XYAccount.username == '',
                        XYAccount.login_password.is_(None),
                        XYAccount.login_password == ''
                    )
                )
        
        # 应用所有条件
        if conditions:
            base_stmt = base_stmt.where(and_(*conditions))
        
        # 查询总数：直接基于条件统计，避免把整表 SELECT 包进子查询
        count_stmt = select(func.count(XYAccount.id))
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar() or 0
        
        # 分页查询：启用账号排在前面，再按创建时间倒序
        from sqlalchemy import case
        inactive_statuses_list = ["inactive", "disabled", "suspended", "deleted"]
        status_order = case(
            (XYAccount.status.in_(inactive_statuses_list), 1),
            else_=0
        )
        offset = (page - 1) * page_size
        stmt = base_stmt.order_by(status_order, XYAccount.created_at.desc()).offset(offset).limit(page_size)
        result = await self.session.execute(stmt)
        
        return list(result.scalars().all()), total

    async def list_all_accounts(self) -> list[XYAccount]:
        """获取所有账号（用于启动时加载）"""
        stmt = select(XYAccount).order_by(XYAccount.account_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_enabled_accounts(self) -> list[XYAccount]:
        """获取所有启用的账号
        
        Returns:
            启用状态的账号列表
        """
        stmt = (
            select(XYAccount)
            .where(XYAccount.status == "active")
            .order_by(XYAccount.account_id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_account_for_user(self, owner_id: int | None, account_identifier: str) -> XYAccount | None:
        """
        获取指定用户的账号
        
        Args:
            owner_id: 用户ID，如果为 None 则不限制用户（管理员模式）
            account_identifier: 账号标识（支持 account_id 或 unb）
            
        Returns:
            账号对象，如果不存在则返回 None
        """
        # 先按 account_id 精确查询（account_id 全局唯一，走 uk_account_id 索引），避免 OR 导致索引失效
        stmt = select(XYAccount).where(XYAccount.account_id == account_identifier)
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        account = result.scalars().first()

        # account_id 未命中时，再按 unb 查询（兼容历史数据）
        if account is None:
            stmt2 = select(XYAccount).where(XYAccount.unb == account_identifier)
            if owner_id is not None:
                stmt2 = stmt2.where(XYAccount.owner_id == owner_id)
            result2 = await self.session.execute(stmt2)
            account = result2.scalars().first()

        return account

    async def get_accounts_for_user(self, owner_id: int | None, account_ids: list[str]) -> list[XYAccount]:
        if not account_ids:
            return []
        stmt = select(XYAccount).where(XYAccount.account_id.in_(account_ids))
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_account_by_identifier(self, account_identifier: str) -> XYAccount | None:
        """根据账号标识获取账号（不限制用户，管理员使用）"""
        stmt = select(XYAccount).where(XYAccount.account_id == account_identifier)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def account_id_exists(self, account_id: str, exclude_pk: int | None = None) -> bool:
        """检查 account_id 是否已存在（全局，不区分所属用户）

        Args:
            account_id: 待校验的账号ID
            exclude_pk: 需排除的账号主键（用于更新场景，排除自身）

        Returns:
            True 表示已存在，False 表示不存在
        """
        stmt = select(func.count(XYAccount.id)).where(XYAccount.account_id == account_id)
        if exclude_pk is not None:
            stmt = stmt.where(XYAccount.id != exclude_pk)
        result = await self.session.execute(stmt)
        return (result.scalar() or 0) > 0

    async def stage_cookie_import(self, owner_id, account_id, cookie_value, *,
                                  create_only=False, enabled=None, profile=None, login_method='manual', commit=True):
        """Save a profile and a fenced verification job atomically; keep candidate secrets out of responses."""
        import re
        from common.utils.xianyu_utils import trans_cookies
        account_id = str(account_id or '').strip()
        if not re.fullmatch(r'[\w.@-]{1,80}', account_id):
            raise ValueError('账号ID格式错误')
        # Serializes quota and profile additions for one owner.
        owner = await self.session.scalar(select(User).where(User.id == owner_id).with_for_update())
        if owner is None:
            raise ValueError('用户不存在')
        account = await self.session.scalar(select(XYAccount).where(
            XYAccount.account_id == account_id).with_for_update().execution_options(populate_existing=True))
        if account is not None and (create_only or account.owner_id != owner_id or account.status == 'deleted'):
            raise ValueError('账号ID已存在，请使用原账号维护入口')
        created = account is None
        candidate = account_policy._secret(cookie_value)
        if candidate is None:
            candidate = account.cookie if account is not None else ''
        identity = trans_cookies(candidate).get('unb')
        if not identity or len(identity) > 64:
            raise ValueError('Cookie 缺少有效账号身份 unb')
        if account is not None and account.unb and str(account.unb) != identity:
            raise ValueError('凭据账号身份不一致')
        aliases = (await self.session.execute(select(XYAccount).where(XYAccount.unb == identity))).scalars().all()
        if any(account is None or other.id != account.id for other in aliases):
            raise ValueError('闲鱼身份已有账号，请使用原账号刷新')
        if created:
            await AccountLimitService(self.session).ensure_can_add_account(owner_id)
            account = XYAccount(owner_id=owner_id, account_id=account_id, unb=identity, cookie='',
                login_method=login_method, status='active' if enabled is not False else 'inactive',
                auto_confirm=False, pause_duration=10, show_browser=False, proxy_type='none')
            self.session.add(account)
        else:
            account.unb = identity
            if enabled is True:
                account.status, account.disable_reason = 'active', None
            elif enabled is False and account.status == 'active':
                account.status, account.disable_reason = 'inactive', 'import_disabled'
        fields = profile or {}
        plain_fields = {'remark','pause_duration','message_expire_time','show_browser',
                        'proxy_type','proxy_host','proxy_port'}
        from common.services.typed_settings import DEFINITIONS, validate_value
        for key in plain_fields & fields.keys():
            value = validate_value(key, fields[key]) if key in DEFINITIONS else fields[key]
            setattr(account, key, value)
        account_policy.update_login_fields(account, username=fields.get('username'),
                                            login_password=fields.get('login_password'))
        for key in ('proxy_user', 'proxy_pass'):
            value = account_policy._secret(fields.get(key))
            if value is not None:
                setattr(account, key, value)
        account_policy.proxy_url({key:getattr(account,key) for key in
            ('proxy_type','proxy_host','proxy_port','proxy_user','proxy_pass')})
        if not created:
            from common.services.typed_settings import clear_inheritance
            clear_inheritance(account, fields)
            state = account_policy.snapshot(account)
            for task in state['jobs'].values():
                if task['status'] not in account_policy.TERMINAL:
                    task['status'] = 'superseded'
            account_policy.store(account, state)
            # Every spreadsheet import is one configuration batch. Later sheets are
            # saved before the queued credential checker starts, while business is paused.
            account_policy.bump_config(account)
        job = account_policy.start_job(account, owner_id=owner_id, kind='cookie_import')
        from sqlalchemy.exc import IntegrityError
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ValueError('账号身份已存在，请刷新原账号') from exc
        if commit:
            await self.session.commit()
            await self.session.refresh(account)
        return account, job, candidate, created

    async def create_account(self, owner_id: int, account_id: str, cookie_value: str, *,
                             unb: str | None = None, login_method: str = 'manual') -> XYAccount:
        account, _, _, _ = await self.stage_cookie_import(owner_id, account_id, cookie_value,
            create_only=True, enabled=True, login_method=login_method)
        return account

    async def update_cookie(self, account: XYAccount, value: str, *, expected_version: int | None = None) -> None:
        if expected_version is None:
            expected_version = account_policy.snapshot(account)['credential_version']
        account = await self._lock_account(account)
        account_policy.replace_credentials(account, value, expected_version=expected_version)
        account.metadata_json = clear_cookie_refresh_snapshot(account.metadata_json)
        self.session.add(account)
        await self.session.commit()

    async def update_status(self, account: XYAccount, enabled: bool, disable_reason: str | None = None) -> None:
        """更新账号状态
        
        Args:
            account: 账号对象
            enabled: 是否启用
            disable_reason: 禁用原因（仅在禁用时有效，启用时会清空）
        """
        account = await self._lock_account(account)
        account.status = "active" if enabled else "disabled"
        account_policy.bump_config(account)
        state = account_policy.snapshot(account)
        state.update(business_state="unchecked" if enabled else "disabled", reason=None if enabled else "manual_disabled")
        account_policy.store(account, state)
        # 启用时清空禁用原因，禁用时设置禁用原因
        account.disable_reason = None if enabled else disable_reason
        self.session.add(account)
        await self.session.commit()

    async def _save_config_fields(self, account: XYAccount, **values) -> None:
        from common.services.typed_settings import clear_inheritance, DEFINITIONS, validate_value
        for key, value in values.items():
            if key in DEFINITIONS:
                validate_value(key, value)
        account = await self._lock_account(account)
        clear_inheritance(account, values)
        for key, value in values.items():
            setattr(account, key, value)
        account_policy.bump_config_preserving_pause(account)
        await self.session.commit()

    async def update_remark(self, account: XYAccount, remark: str) -> None:
        account.remark = remark
        self.session.add(account)
        await self.session.commit()

    async def update_auto_confirm(self, account: XYAccount, auto_confirm: bool) -> None:
        values = {'auto_confirm': auto_confirm}
        if auto_confirm:
            values['only_send_card'] = False
        await self._save_config_fields(account, **values)

    async def update_pause_duration(self, account: XYAccount, duration: int) -> None:
        await self._save_config_fields(account, pause_duration=duration)

    async def update_message_expire_time(self, account: XYAccount, expire_time: int) -> None:
        await self._save_config_fields(account, message_expire_time=expire_time)

    async def update_reply_delay(self, account: XYAccount, delay_seconds: int) -> None:
        await self._save_config_fields(account, reply_delay_seconds=delay_seconds)

    async def update_login_info(
        self,
        account: XYAccount,
        username: str | None = None,
        login_password: str | None = None,
        show_browser: bool | None = None,
        clear_fields: list[str] | None = None,
    ) -> None:
        """空白/脱敏字段保持原值；只有 clear_fields 显式清除。"""
        account = await self._lock_account(account)
        before = (account.username, account.login_password, account.show_browser)
        was_inherited = 'show_browser' in account_policy.snapshot(account).get('inherited_fields', [])
        account_policy.update_login_fields(account, username=username, login_password=login_password, clear_fields=clear_fields or ())
        if show_browser is not None:
            account.show_browser = show_browser
            from common.services.typed_settings import clear_inheritance
            clear_inheritance(account, ['show_browser'])
        if before != (account.username, account.login_password, account.show_browser) or (show_browser is not None and was_inherited):
            account_policy.bump_config_preserving_pause(account)
        self.session.add(account)
        await self.session.commit()

    async def update_scheduled_redelivery(self, account: XYAccount, scheduled_redelivery: bool) -> None:
        await self._save_config_fields(account, scheduled_redelivery=scheduled_redelivery)

    async def update_scheduled_rate(self, account: XYAccount, scheduled_rate: bool) -> None:
        await self._save_config_fields(account, scheduled_rate=scheduled_rate)

    async def delete_preview(self, account):
        from sqlalchemy import or_
        from common.models.xy_order import XYOrder
        from common.models.delivery_intent import DeliveryIntent
        from common.models.account_operation import AccountOperation
        from common.models.reply_state import reply_outbox

        async def count_rows(table, *conditions):
            return await self.session.scalar(select(func.count()).select_from(table).where(*conditions)) or 0

        counts = {
            'unfinished_orders': await count_rows(XYOrder, XYOrder.account_id == account.account_id,
                XYOrder.owner_id == account.owner_id,
                XYOrder.status.not_in(('completed', 'closed', 'cancelled', 'refunded'))),
            'unfinished_deliveries': await count_rows(DeliveryIntent, DeliveryIntent.account_id == account.account_id,
                DeliveryIntent.owner_id == account.owner_id, or_(
                    DeliveryIntent.content_state.not_in(('confirmed', 'not_sent')),
                    DeliveryIntent.confirm_state.not_in(('confirmed', 'not_required')))),
            'pending_replies': await count_rows(reply_outbox, reply_outbox.c.account_id == account.account_id,
                reply_outbox.c.status.in_(('submitted', 'unknown'))),
            'pending_operations': await count_rows(AccountOperation, AccountOperation.account_id == account.account_id,
                AccountOperation.owner_id == account.owner_id, AccountOperation.status.in_(('submitted', 'unknown'))),
        }
        state = account_policy.snapshot(account)
        counts['active_jobs'] = sum(j['status'] not in account_policy.TERMINAL for j in state['jobs'].values())
        return {'account_id': account.account_id, **counts, 'can_delete': not any(counts.values()),
                'retains_business_history': True}

    async def delete_account(self, account: XYAccount) -> None:
        account = await self._lock_account(account)
        preview = await self.delete_preview(account)
        if not preview['can_delete']:
            raise ValueError('账号仍有未完成订单、履约、消息或凭据任务，请先核实')
        # 软删除保留业务事实、归属和审计；不级联清理订单。
        account.status = 'deleted'
        account.disable_reason = '用户删除'
        account_policy.bump_config(account)
        await self.session.commit()

    async def get_account_by_unb(self, owner_id: int, unb: str) -> XYAccount | None:
        stmt = select(XYAccount).where(
            XYAccount.owner_id == owner_id,
            XYAccount.unb == unb,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _generate_unique_account_id(self, owner_id: int, base: str) -> str:
        # 全局唯一：account_id 在整个系统内不允许重复，生成候选时不区分 owner_id
        normalized = base or f"qr_{int(datetime.utcnow().timestamp())}"
        stmt = select(XYAccount.account_id)
        result = await self.session.execute(stmt)
        existing_ids = set(result.scalars().all())
        candidate = normalized
        counter = 1
        while candidate in existing_ids:
            candidate = f"{normalized}_{counter}"
            counter += 1
        return candidate

    async def upsert_account_from_qr(
        self,
        owner_id: int,
        cookies: str,
        unb: str | None,
        *,
        login_method: str = "qr_scan",
        target_account_id: str | None = None,
        expected_version: int | None = None,
    ) -> tuple[XYAccount, bool]:
        await self._check_identity_owner(owner_id, unb)
        account: XYAccount | None = None
        if target_account_id:
            account = await self.get_account_for_user(owner_id, target_account_id)
            if account is None:
                raise ValueError("刷新目标账号不存在")
        elif unb:
            account = await self.get_account_by_unb(owner_id, unb)

        created = False
        if account:
            account = await self._lock_account(account)
            account_policy.replace_credentials(account, cookies, expected_version=expected_version)
            account.metadata_json = clear_cookie_refresh_snapshot(account.metadata_json)
            account.login_method = login_method
            account.unb = unb
            account.last_login_at = datetime.now(tz=UTC)
            if hasattr(account, "updated_at"):
                account.updated_at = datetime.now(tz=UTC)
        else:
            await AccountLimitService(self.session).ensure_can_add_account(owner_id)
            base_id = unb or f"qr_{int(datetime.utcnow().timestamp())}"
            new_id = await self._generate_unique_account_id(owner_id, base_id)
            account = XYAccount(
                owner_id=owner_id,
                account_id=new_id,
                cookie=cookies,
                login_method=login_method,
                status="active",
                auto_confirm=False,
                pause_duration=10,
                show_browser=False,
                unb=unb,
                last_login_at=datetime.now(tz=UTC),
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
            self.session.add(account)
            created = True

        self.session.add(account)
        await self.session.commit()
        if created:
            await self.session.refresh(account)
        return account, created

    async def upsert_account_from_password(
        self,
        owner_id: int,
        account_id: str,
        account: str,
        password: str,
        cookies: str,
        unb: str | None,
        show_browser: bool = False,
    ) -> tuple[XYAccount, bool]:
        """协议化账号密码登录成功后保存账号（按前端传入的 account_id upsert）。

        与扫码登录 upsert_account_from_qr 的差异：按 account_id（非 unb）定位，
        并保存 username/login_password/show_browser/login_method='password'。

        Args:
            owner_id: 所属用户ID
            account_id: 前端传入的业务账号ID（定位键）
            account: 登录账号（手机号/邮箱），写入 username
            password: 明文登录密码，写入 login_password（供后台自动刷新复用）
            cookies: 登录成功后的 Cookie 字符串
            unb: 从 Cookie 提取的 unb
            show_browser: 是否有头模式（沿用请求传入值）
        Returns:
            (账号对象, 是否新建)
        Raises:
            ValueError: account_id 已被其他用户占用
        """
        await self._check_identity_owner(owner_id, unb)
        existing = await self.get_account_by_identifier(account_id)
        # account_id 已被其他用户占用时禁止覆盖，直接报错（与 websocket 版一致）
        if existing and existing.owner_id != owner_id:
            raise ValueError(f"账号ID {account_id} 已被其他用户占用，无法登录")

        now = datetime.now(tz=UTC)
        created = False
        if existing:
            existing = await self._lock_account(existing)
            account_policy.replace_credentials(existing, cookies)
            existing.metadata_json = clear_cookie_refresh_snapshot(existing.metadata_json)
            account_policy.update_login_fields(existing, username=account, login_password=password)
            existing.show_browser = show_browser
            existing.login_method = "password"
            existing.last_login_at = now
            if unb:
                existing.unb = unb
            if hasattr(existing, "updated_at"):
                existing.updated_at = now
            account_obj = existing
        else:
            await AccountLimitService(self.session).ensure_can_add_account(owner_id)
            account_obj = XYAccount(
                owner_id=owner_id,
                account_id=account_id,
                cookie=cookies,
                username=account,
                login_password=password,
                show_browser=show_browser,
                login_method="password",
                status="active",
                auto_confirm=False,
                pause_duration=10,
                unb=unb,
                last_login_at=now,
                created_at=now,
                updated_at=now,
            )
            created = True

        self.session.add(account_obj)
        await self.session.commit()
        if created:
            await self.session.refresh(account_obj)
        return account_obj, created

    async def update_auto_polish(self, account: XYAccount, auto_polish: bool) -> None:
        await self._save_config_fields(account, auto_polish=auto_polish)

    async def update_confirm_before_send(self, account: XYAccount, confirm_before_send: bool) -> None:
        values = {'confirm_before_send': confirm_before_send}
        if confirm_before_send:
            values.update(send_before_confirm=False, only_send_card=False)
        await self._save_config_fields(account, **values)

    async def update_send_before_confirm(self, account: XYAccount, send_before_confirm: bool) -> None:
        values = {'send_before_confirm': send_before_confirm}
        if send_before_confirm:
            values.update(confirm_before_send=False, only_send_card=False)
        await self._save_config_fields(account, **values)

    async def update_only_send_card(self, account: XYAccount, only_send_card: bool) -> None:
        values = {'only_send_card': only_send_card}
        if only_send_card:
            values.update(auto_confirm=False, confirm_before_send=False, send_before_confirm=False)
        await self._save_config_fields(account, **values)

    async def update_auto_red_flower(self, account: XYAccount, auto_red_flower: bool) -> None:
        await self._save_config_fields(account, auto_red_flower=auto_red_flower)

    async def update_ai_reply_block_ordered_users(self, account: XYAccount, ai_reply_block_ordered_users: bool) -> None:
        await self._save_config_fields(account, ai_reply_block_ordered_users=ai_reply_block_ordered_users)

    async def update_delivery_disabled(
        self,
        account: XYAccount,
        delivery_disabled: bool,
        delivery_disabled_reason: str | None,
        auto_close_order: bool = False,
        delivery_only_card_after_close: bool = False,
        excluded_item_ids: list[str] | None = None,
    ) -> None:
        """Save account fields and the active rule together, using one configuration version."""
        account = await self._lock_account(account)
        from common.services.typed_settings import clear_inheritance
        from common.services.delivery_rule_configuration import sync_legacy_credit_rule
        account.delivery_disabled = delivery_disabled
        account.delivery_disabled_reason = delivery_disabled_reason
        account.auto_close_order = auto_close_order
        account.delivery_only_card_after_close = delivery_only_card_after_close
        account.delivery_disabled_excluded_items = excluded_item_ids
        await sync_legacy_credit_rule(self.session, account)
        clear_inheritance(account, ['delivery_disabled_reason'])
        account_policy.bump_config_preserving_pause(account)
        await self.session.commit()

    async def get_delivery_block_rules(self, account_id: str) -> list[dict]:
        """获取账号的禁止发货规则列表

        返回该账号所有规则配置（包括未启用的），按 priority 排序。
        如果账号在 xy_delivery_block_rules 表中没有记录，返回所有可用规则的默认配置。

        Args:
            account_id: 账号标识（xy_accounts.account_id）

        Returns:
            规则配置列表
        """
        from common.models.xy_delivery_block_rule import XYDeliveryBlockRule
        from common.services.delivery_block_rule_meta import get_all_rule_metadata

        # 查询已有规则
        stmt = (
            select(XYDeliveryBlockRule)
            .where(XYDeliveryBlockRule.account_id == account_id)
            .order_by(XYDeliveryBlockRule.priority.asc())
        )
        result = await self.session.execute(stmt)
        existing_rules = result.scalars().all()

        # 构建已有规则的 code 集合
        existing_codes = {r.rule_code for r in existing_rules}

        # 获取所有可用规则元信息
        all_metadata = get_all_rule_metadata()

        # 合并：已有规则 + 未配置的规则（用默认值填充）
        rule_list = []
        for rule in existing_rules:
            # 归一化 excluded_item_ids
            excluded = []
            if rule.excluded_item_ids:
                raw = rule.excluded_item_ids
                if isinstance(raw, str):
                    try:
                        import json
                        raw = json.loads(raw)
                    except Exception:
                        raw = []
                if isinstance(raw, list):
                    excluded = [str(x).strip() for x in raw if x is not None and str(x).strip()]

            rule_list.append({
                "rule_code": rule.rule_code,
                "rule_name": next(
                    (m["rule_name"] for m in all_metadata if m["rule_code"] == rule.rule_code),
                    rule.rule_code,
                ),
                "rule_description": next(
                    (m["rule_description"] for m in all_metadata if m["rule_code"] == rule.rule_code),
                    "",
                ),
                "enabled": rule.enabled,
                "priority": rule.priority,
                "block_reason": rule.block_reason or "",
                "auto_close_order": bool(rule.auto_close_order),
                "only_card_after_close": bool(rule.only_card_after_close),
                "excluded_item_ids": excluded,
                "config": rule.config or {},
                "default_config": next(
                    (m["default_config"] for m in all_metadata if m["rule_code"] == rule.rule_code),
                    {},
                ),
            })

        # 补充未配置的规则（默认关闭）
        for meta in all_metadata:
            if meta["rule_code"] not in existing_codes:
                rule_list.append({
                    "rule_code": meta["rule_code"],
                    "rule_name": meta["rule_name"],
                    "rule_description": meta["rule_description"],
                    "enabled": False,
                    "priority": meta["default_priority"],
                    "block_reason": "",
                    "auto_close_order": False,
                    "only_card_after_close": False,
                    "excluded_item_ids": [],
                    "config": meta["default_config"],
                    "default_config": meta["default_config"],
                })

        # 按 priority 排序
        rule_list.sort(key=lambda x: x["priority"])
        return rule_list

    async def update_delivery_block_rules(
        self,
        account_id: str,
        rules: list,
    ) -> None:
        """批量更新账号的禁止发货规则配置

        使用 UPSERT 逻辑：存在则更新，不存在则插入。

        Args:
            account_id: 账号标识（xy_accounts.account_id）
            rules: 规则配置列表（DeliveryBlockRuleItem 实例列表）
        """
        from common.models.xy_delivery_block_rule import XYDeliveryBlockRule
        from sqlalchemy import and_

        account = await self.session.scalar(select(XYAccount).where(
            XYAccount.account_id == account_id).with_for_update().execution_options(populate_existing=True))
        if account is None:
            raise ValueError("account_not_found")

        for rule_item in rules:
            rule_code = rule_item.rule_code
            enabled = rule_item.enabled
            priority = rule_item.priority
            block_reason = (rule_item.block_reason or "").strip() or None
            if rule_code == 'buyer_credit_zero':
                from common.services.typed_settings import clear_inheritance
                clear_inheritance(account, ['delivery_disabled_reason'])
                account.delivery_disabled_reason = block_reason
            auto_close = rule_item.auto_close_order
            only_card = rule_item.only_card_after_close if auto_close else False

            # 归一化排除商品列表
            excluded_list: list[str] = []
            if rule_item.excluded_item_ids:
                seen: set[str] = set()
                for raw in rule_item.excluded_item_ids:
                    if raw is None:
                        continue
                    item_id = str(raw).strip()
                    if not item_id or item_id in seen:
                        continue
                    seen.add(item_id)
                    excluded_list.append(item_id)
                    if len(excluded_list) >= 500:
                        break
            excluded_for_db = excluded_list if excluded_list else None

            # 规则参数
            config = rule_item.config if rule_item.config else None

            # 查询是否已存在
            stmt = select(XYDeliveryBlockRule).where(
                and_(
                    XYDeliveryBlockRule.account_id == account_id,
                    XYDeliveryBlockRule.rule_code == rule_code,
                )
            )
            result = await self.session.execute(stmt)
            existing = result.scalars().first()

            if existing:
                # 更新
                existing.enabled = enabled
                existing.priority = priority
                existing.block_reason = block_reason
                existing.auto_close_order = auto_close
                existing.only_card_after_close = only_card
                existing.excluded_item_ids = excluded_for_db
                existing.config = config
            else:
                # 插入
                new_rule = XYDeliveryBlockRule(
                    account_id=account_id,
                    rule_code=rule_code,
                    enabled=enabled,
                    priority=priority,
                    block_reason=block_reason,
                    auto_close_order=auto_close,
                    only_card_after_close=only_card,
                    excluded_item_ids=excluded_for_db,
                    config=config,
                )
                self.session.add(new_rule)

        # 同步更新旧字段 delivery_disabled（用于前端图标显示兼容）
        # 只要有任何一条规则 enabled=True，旧字段就标记为 True
        has_any_enabled = any(r.enabled for r in rules)
        sync_stmt = (
            update(XYAccount)
            .where(XYAccount.account_id == account_id)
            .values(delivery_disabled=has_any_enabled)
        )
        await self.session.execute(sync_stmt)

        account_policy.bump_config_preserving_pause(account)
        await self.session.commit()
