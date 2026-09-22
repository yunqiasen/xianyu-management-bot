"""
商品发布执行与日志服务

功能：
1. 提供商品发布日志的创建、更新、分页查询
2. 提供单品发布与批量发布执行能力
3. 在发布前解析随机地址池，并记录地址来源到日志
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.paths import STATIC_ROOT
from app.services.publish_address_service import PublishAddressService
from app.services.publish_batch_status_service import PublishBatchStatusService
from app.services.item_service import ItemService
from common.models.publish_log import PublishLog
from common.services.publish_log_service import PublishLogService as CommonPublishLogService
from common.models.xy_account import XYAccount
from common.services.publish_execution_service import (
    SYNC_AFTER_PUBLISH_DELAY_SECONDS,
    execute_single_publish,
)
from common.services.xianyu_publish_service import (
    detect_publish_account_capability,
    ensure_publish_capability_reliable,
    publish_personal_single_item,
    publish_single_item,
)
from common.utils.xianyu_utils import canonical_goofish_item_url


from common.utils.time_utils import safe_isoformat
class PublishLogService(CommonPublishLogService):
    """Web and scheduler share snapshot and idempotency persistence."""



class PublishExecutorService:
    """商品发布执行服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_account(self, account_id: str, user_id: int) -> Optional[XYAccount]:
        stmt = select(XYAccount).where(
            XYAccount.account_id == account_id,
            XYAccount.owner_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _get_account_cookie(self, account_id: str, user_id: int) -> Optional[str]:
        """获取账号 Cookie 字符串（验证归属）"""
        account = await self._get_account(account_id, user_id)
        if not account:
            return None
        return account.cookie

    async def _get_account_map(self, account_ids: List[str], user_id: int) -> Dict[str, XYAccount]:
        """批量获取账号对象映射"""
        if not account_ids:
            return {}
        unique_ids = list(dict.fromkeys(account_ids))
        stmt = (
            select(XYAccount)
            .where(
                XYAccount.owner_id == user_id,
                XYAccount.account_id.in_(unique_ids),
            )
            .order_by(desc(XYAccount.id))
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        account_map: Dict[str, XYAccount] = {}
        for row in rows:
            if row.account_id not in account_map:
                account_map[row.account_id] = row
        return account_map

    async def _sync_account_items_after_publish(self, account_id: str, account: XYAccount) -> Dict[str, Any]:
        item_svc = ItemService(self.session)
        # 平台商品列表有索引延迟，最后一件商品发布后没有间隔就同步会漏掉它，先等待再拉取
        if SYNC_AFTER_PUBLISH_DELAY_SECONDS > 0:
            logger.info(
                f"账号 {account_id} 批量发布完成，等待 {SYNC_AFTER_PUBLISH_DELAY_SECONDS} 秒后再自动获取商品"
            )
            await asyncio.sleep(SYNC_AFTER_PUBLISH_DELAY_SECONDS)
        try:
            sync_result = await item_svc.fetch_all_items_from_account(account=account)
            sync_status = "success" if sync_result.get("success") else "failed"
            sync_total_count = int(sync_result.get("total_count") or 0)
            sync_saved_count = int(sync_result.get("saved_count") or 0)
            if sync_status == "success":
                sync_message = f"已自动获取 {sync_total_count} 个商品，入库 {sync_saved_count} 个商品"
                logger.info(
                    f"账号 {account_id} 发布后自动获取商品完成：共 {sync_total_count} 件，保存 {sync_saved_count} 件"
                )
            else:
                sync_message = f"自动获取商品失败：{sync_result.get('message') or '未知错误'}"
                logger.warning(
                    f"账号 {account_id} 发布后自动获取商品失败，不影响后续发布：{sync_result.get('message', '未知错误')}"
                )
            return {
                "sync_status": sync_status,
                "sync_message": sync_message,
                "sync_total_count": sync_total_count,
                "sync_saved_count": sync_saved_count,
            }
        except Exception as sync_exc:
            logger.warning(
                f"账号 {account_id} 发布后自动获取商品异常，不影响后续发布：{sync_exc}"
            )
            return {
                "sync_status": "failed",
                "sync_message": f"自动获取商品异常：{sync_exc}",
                "sync_total_count": 0,
                "sync_saved_count": 0,
            }

    async def publish_single(
        self,
        user_id: int,
        account_id: str,
        item_data: dict,
    ) -> Dict[str, Any]:
        """单品发布"""
        return await execute_single_publish(
            session=self.session,
            user_id=user_id,
            account_id=account_id,
            item_data=item_data,
            static_root=STATIC_ROOT,
            publish_request_id=item_data.get("publish_request_id"),
        )

    async def batch_publish(
        self,
        user_id: int,
        account_ids: List[str],
        materials: List[dict],
        batch_id: str = None,
    ) -> Dict[str, Any]:
        """All Web/WS batch callers use the persistent queue and common single executor."""
        from common.services.product_batch_service import ProductBatchService
        svc = ProductBatchService(self.session)
        if batch_id is None or not await svc.get(user_id, batch_id):
            batch = await svc.create(user_id, account_ids, materials, batch_id)
            batch_id = batch.id
        return await svc.run(user_id, batch_id, STATIC_ROOT)


def _log_to_dict(log: PublishLog) -> dict:
    """将发布日志模型转为字典"""
    item_url = log.item_url
    if log.item_id:
        item_url = canonical_goofish_item_url(log.item_id)
    return {
        "id": log.id,
        "user_id": log.user_id,
        "account_id": log.account_id,
        "title": log.title,
        "description": log.description,
        "price": log.price,
        "material_id": log.material_id,
        "batch_id": log.batch_id,
        "status": log.status,
        "item_url": item_url,
        "item_id": log.item_id,
        "error_message": log.error_message,
        "resolved_address_id": log.resolved_address_id,
        "resolved_address_text": log.resolved_address_text,
        "address_source": log.address_source,
        "created_at": safe_isoformat(log.created_at),
        "updated_at": safe_isoformat(log.updated_at),
    }
