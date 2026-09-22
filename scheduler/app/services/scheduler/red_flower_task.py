"""
定时求小红花任务

功能：
1. 查询所有开启自动求小红花的账号
2. 获取每个账号近10天内未求小红花的订单
3. 调用闲鱼求小红花API
4. 处理响应Set-Cookie，合并更新到数据库
5. 令牌过期时从Set-Cookie提取新Cookie，更新数据库后自动重试一次
6. Session过期时标记冷却并触发后台密码登录
7. 更新订单 is_red_flower 字段
8. 记录执行日志
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import aiohttp
from loguru import logger
from sqlalchemy import delete as sql_delete, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from common.db.session import async_session_maker
from common.models.scheduled_red_flower_log import ScheduledRedFlowerLog
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.utils.time_utils import get_beijing_now_naive
from common.utils.xianyu_utils import trans_cookies, generate_sign
from common.utils.cookie_refresh import (
    is_token_expired_error, handle_token_expired_response,
    update_account_cookies_in_db,
    is_session_expired_error, trigger_password_login_async,
    mark_account_session_expired, extract_cookies_from_response,
    merge_cookies, is_account_session_cooled,
)


# 全局冷却缓存：订单号 -> 冷却过期时间
_order_cooldown_cache: Dict[str, datetime] = {}

# 冷却时间（秒）
ORDER_COOLDOWN_SECONDS = 600  # 10分钟


def is_order_in_cooldown(order_no: str) -> bool:
    """检查订单是否在冷却期内"""
    if order_no not in _order_cooldown_cache:
        return False
    expire_time = _order_cooldown_cache[order_no]
    if datetime.now() >= expire_time:
        del _order_cooldown_cache[order_no]
        return False
    return True


def add_order_to_cooldown(order_no: str, cooldown_seconds: int = ORDER_COOLDOWN_SECONDS) -> None:
    """将订单加入冷却缓存"""
    expire_time = datetime.now() + timedelta(seconds=cooldown_seconds)
    _order_cooldown_cache[order_no] = expire_time
    logger.info(f"[求小红花] 订单 {order_no} 加入冷却 {cooldown_seconds}秒")


def cleanup_expired_cooldowns() -> None:
    """清理已过期的冷却记录"""
    now = datetime.now()
    expired_orders = [
        order_no for order_no, expire_time in _order_cooldown_cache.items()
        if now >= expire_time
    ]
    for order_no in expired_orders:
        del _order_cooldown_cache[order_no]
    if expired_orders:
        logger.info(f"[求小红花] 清理了 {len(expired_orders)} 个过期冷却记录")


class RedFlowerTask:
    """定时求小红花任务"""

    # 订单处理间隔（秒），避免请求过快被限流
    ORDER_PROCESS_DELAY = 0.5
    # HTTP请求超时（秒）
    REQUEST_TIMEOUT = 30
    # 求小红花日志保留天数，超过该天数的日志在每次任务执行时主动清理
    LOG_RETENTION_DAYS = 10

    async def execute(self) -> str:
        """
        执行求小红花任务

        Returns:
            批次ID
        """
        batch_id = str(uuid.uuid4())
        logger.info(f"[求小红花] 开始执行，批次ID: {batch_id}")

        # 清理过期冷却记录
        cleanup_expired_cooldowns()

        try:
            async with async_session_maker() as session:
                # 主动清理过期的求小红花日志（10天前）
                await self._cleanup_expired_logs(session)

                # 获取符合条件的账号
                accounts = await self._get_eligible_accounts(session)

                if not accounts:
                    logger.info("[求小红花] 没有符合条件的账号")
                    return batch_id

                logger.info(f"[求小红花] 找到 {len(accounts)} 个符合条件的账号")

                # 处理每个账号
                for account in accounts:
                    try:
                        await self._process_account(session, batch_id, account)
                    except Exception as e:
                        logger.error(f"[求小红花] 处理账号 {account.account_id} 异常: {e}")
                        continue

        except Exception as e:
            logger.error(f"[求小红花] 执行异常: {e}")

        return batch_id

    async def _cleanup_expired_logs(self, session: AsyncSession) -> None:
        """
        主动清理过期的求小红花日志

        删除 created_at 早于 (当前北京时间 - LOG_RETENTION_DAYS 天) 的日志记录，
        避免日志表无限增长。使用参数化的 ORM delete 语句，避免 SQL 注入。
        """
        try:
            cutoff_time = get_beijing_now_naive() - timedelta(days=self.LOG_RETENTION_DAYS)
            stmt = sql_delete(ScheduledRedFlowerLog).where(
                ScheduledRedFlowerLog.created_at < cutoff_time
            )
            result = await session.execute(stmt)
            await session.commit()

            deleted_count = result.rowcount or 0
            if deleted_count > 0:
                logger.info(
                    f"[求小红花] 已清理 {deleted_count} 条 {self.LOG_RETENTION_DAYS} 天前的求小红花日志"
                    f"（清理时间界限: {cutoff_time}）"
                )
        except Exception as e:
            logger.error(f"[求小红花] 清理过期日志失败: {e}")
            await session.rollback()

    async def _get_eligible_accounts(self, session: AsyncSession) -> List[XYAccount]:
        """
        获取符合条件的账号列表

        条件：
        - status = 'active'（启用）
        - auto_red_flower = True（自动求小红花开启）
        """
        stmt = select(XYAccount).where(
            XYAccount.status == "active",
            XYAccount.auto_red_flower == True,
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # 不需要求小红花的订单状态（已取消、处理中、退款中/已退款）
    EXCLUDED_ORDER_STATUSES = ("cancelled", "processing", "refunding", "refunded")

    async def _get_pending_orders(
        self,
        session: AsyncSession,
        account_id: str,
    ) -> List[XYOrder]:
        """
        获取近10天内未求小红花的订单

        条件：
        - account_id 匹配
        - 真实下单时间(placed_at)在近10天内（不是数据库写入时间，
          避免同步历史订单时 created_at 都是最近导致"全量处理"问题）
        - placed_at 不为 NULL（历史空值数据跳过，防误伤）
        - is_red_flower = False
        - 排除已取消(cancelled)和待付款(processing)的订单
        - 按下单时间升序排列
        """
        now = get_beijing_now_naive()
        ten_days_ago = now - timedelta(days=10)

        stmt = select(XYOrder).where(
            XYOrder.account_id == account_id,
            XYOrder.is_red_flower == False,
            XYOrder.status.notin_(self.EXCLUDED_ORDER_STATUSES),
            XYOrder.placed_at.is_not(None),
            XYOrder.placed_at >= ten_days_ago,
        ).order_by(XYOrder.placed_at)

        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _process_account(
        self,
        session: AsyncSession,
        batch_id: str,
        account: XYAccount,
    ) -> None:
        """处理单个账号"""
        from common.services.product_admission import product_admission
        admission = product_admission(account)
        if not admission['allowed']:
            logger.info("账号运营任务跳过: {} {}", account.account_id, admission['status'])
            return
        account_id = account.account_id

        # 检查账号是否处于Session过期冷却期内
        if is_account_session_cooled(account_id):
            logger.info(f"[求小红花] 账号 {account_id} 处于Session过期冷却期内，跳过")
            return

        logger.info(f"[求小红花] 开始处理账号: {account_id}")

        # 获取待处理订单
        orders = await self._get_pending_orders(session, account_id)

        if not orders:
            logger.info(f"[求小红花] 账号 {account_id} 没有待求小红花的订单")
            return

        logger.info(f"[求小红花] 账号 {account_id} 找到 {len(orders)} 个待求小红花订单")

        # 使用可变的cookie字符串，方便set-cookie刷新后后续订单使用最新cookie
        current_cookie_str = account.cookie

        # 处理每个订单
        for order in orders:
            from common.services.product_feedback_policy import feedback_eligibility
            eligibility = feedback_eligibility(account, order, kind='red_flower',
                                               cooling=False)
            if eligibility != 'ready':
                await self._log_result(session, batch_id, account_id, order.order_no,
                                       False, f"skipped:{eligibility}")
                continue

            try:
                success, error_message, updated_cookie = await self._request_red_flower(
                    session, account_id, current_cookie_str, order,
                )

                # 如果cookie被刷新了，同步更新本地变量供后续订单使用
                if updated_cookie and updated_cookie != current_cookie_str:
                    current_cookie_str = updated_cookie
                    logger.info(f"[求小红花] 账号 {account_id} Cookie已通过Set-Cookie更新")

                if success:
                    # 更新订单 is_red_flower 字段
                    await session.execute(
                        sql_update(XYOrder)
                        .where(XYOrder.id == order.id, XYOrder.account_id == account_id, XYOrder.owner_id == account.owner_id)
                        .values(is_red_flower=True)
                    )
                    await session.commit()
                else:
                    # Session过期时，标记冷却并触发密码登录，该账号后续订单都跳过
                    if error_message and "SESSION_EXPIRED" in error_message:
                        await self._log_result(
                            session, batch_id, account_id,
                            order.order_no, False, error_message,
                        )
                        break

                    # 令牌过期且重试仍失败，标记冷却并触发密码登录，该账号后续订单都跳过
                    if error_message and "TOKEN_RETRY_FAILED" in error_message:
                        await self._log_result(
                            session, batch_id, account_id,
                            order.order_no, False, error_message,
                        )
                        break

                    # 临时性错误加入冷却
                    if error_message and self._should_cooldown(error_message):
                        add_order_to_cooldown(order.order_no)

                # 记录日志
                await self._log_result(
                    session, batch_id, account_id,
                    order.order_no, success, error_message,
                )

                # 订单处理间隔
                await asyncio.sleep(self.ORDER_PROCESS_DELAY)

            except Exception as e:
                error_msg = str(e)
                logger.error(f"[求小红花] 处理订单 {order.order_no} 异常: {error_msg}")
                if self._should_cooldown(error_msg):
                    add_order_to_cooldown(order.order_no)
                await self._log_result(
                    session, batch_id, account_id,
                    order.order_no, False, error_msg,
                )
                continue

    async def _request_red_flower(self, session, account_id, cookie_str, order, is_retry=False):
        from common.services.product_feedback_service import ProductFeedbackService
        account = (await session.execute(select(XYAccount).where(XYAccount.account_id == account_id,
            XYAccount.owner_id == order.owner_id))).scalar_one_or_none()
        if not account:
            return False, 'skipped:identity_mismatch', cookie_str
        result = await ProductFeedbackService(session).run_order(account, order, 'red_flower')
        message = ('skipped:' + str(result.get('reason'))) if result['status'] == 'skipped' else result.get('message')
        if result['status'] == 'unknown': message = 'unknown:' + str(message)
        return result['success'], message, cookie_str

    async def _log_result(
        self,
        session: AsyncSession,
        batch_id: str,
        account_id: str,
        order_no: str,
        success: bool,
        error_message: Optional[str],
    ) -> None:
        """记录执行日志"""
        try:
            log = ScheduledRedFlowerLog(
                batch_id=batch_id,
                account_id=account_id,
                order_no=order_no,
                status="unknown" if error_message and error_message.startswith("unknown:") else "skipped" if error_message and error_message.startswith("skipped:") else ("success" if success else "failed"),
                error_message=error_message[:500] if error_message else None,
            )
            session.add(log)
            await session.commit()
        except Exception as e:
            logger.error(f"[求小红花] 记录日志失败: {e}")
            try:
                await session.rollback()
            except Exception:
                pass

    @staticmethod
    def _should_cooldown(error_message: str) -> bool:
        """判断错误是否应该加入冷却队列"""
        if not error_message:
            return False
        error_lower = error_message.lower()
        # 临时性错误需要冷却
        temp_keywords = [
            "token", "令牌", "expired", "超时", "timeout",
            "繁忙", "busy", "限流", "rate limit",
            "网络", "network", "unavailable", "频繁", "frequent",
        ]
        for kw in temp_keywords:
            if kw in error_lower:
                return True
        return False
