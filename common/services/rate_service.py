"""
闲鱼评价服务（公共模块）

功能：
1. 自动评价买家
2. 更新订单评价状态
3. 根据账号配置获取评价内容
4. 检查商品是否属于指定账号

说明：
此模块放在common目录下，供scheduler和websocket共同使用
"""
import json
import time
import asyncio
from typing import Optional, Dict, Any

import aiohttp
from loguru import logger

from common.utils.xianyu_utils import generate_sign


class RateService:
    """闲鱼评价服务
    
    支持令牌过期自动刷新Cookie并重试
    """
    
    def __init__(self, cookie_string: str, account_id: str = None):
        """初始化评价服务
        
        Args:
            cookie_string: 账号Cookie字符串
            account_id: 账号ID，用于令牌过期时更新数据库Cookie（可选）
        """
        self.cookie_string = cookie_string
        self.account_id = account_id
        self.cookies_dict = self._parse_cookies(cookie_string)
    
    def _parse_cookies(self, cookies_str: str) -> dict:
        """解析Cookie字符串为字典"""
        if not cookies_str:
            return {}
        cookies = {}
        for cookie in cookies_str.split("; "):
            if "=" in cookie:
                key, value = cookie.split("=", 1)
                cookies[key.strip()] = value.strip()
        return cookies
    
    async def rate_buyer(self, trade_id: str, feedback: str = "不错的买家", is_retry: bool = False):
        from common.services.product_feedback_service import guarded_rate
        return await guarded_rate(self, trade_id, feedback)

    async def _rate_buyer_impl(self, trade_id: str, feedback: str = "不错的买家", is_retry: bool = False) -> Dict[str, Any]:
        """Submit through the existing account executor; recovery never runs here."""
        from common.services.account_business_client import dispatch_business
        from common.db.session import async_session_maker
        from common.models.xy_account import XYAccount
        from sqlalchemy import select
        async with async_session_maker() as session:
            account = await session.scalar(select(XYAccount).where(XYAccount.account_id == self.account_id))
            if account is None:
                return {'success':False,'definitive_failure':True,'message':'account_not_found'}
        return await dispatch_business(account,'rate_buyer',{'order_no':trade_id,'feedback':feedback})


async def fetch_merchant_rate_list(cookie_string: str, account_id: str = None, page: int = 1, page_size: int = 20, max_retries: int = 3) -> Dict[str, Any]:
    """Read one page via the sole executor; max_retries is a compatibility argument only."""
    from common.db.session import async_session_maker
    from common.models.xy_account import XYAccount
    from common.services.xianyu_mtop import mtop_call
    from sqlalchemy import select

    def failed(code, response=None):
        response = response or {}
        return {'success':False, 'items':[], 'total_count':0, 'message':code,
                'cookies_str':cookie_string, 'retry_after':response.get('retry_after'),
                'account_invalid':bool(response.get('account_invalid')),
                '_request_status_unknown':bool(response.get('_request_status_unknown'))}

    if (not account_id or type(page) is not int or type(page_size) is not int
            or page < 1 or not 1 <= page_size <= 100):
        return failed('invalid_rate_page')
    async with async_session_maker() as session:
        account = await session.scalar(select(XYAccount).where(XYAccount.account_id == account_id))
        if account is None:
            return failed('account_not_found')
        owner_id = account.owner_id
    response = await mtop_call(account_id, cookie_string, 'mtop.taobao.idle.merchant.rate.list', '1.0',
        {'pageNumber':page, 'rowsPerPage':page_size, 'queryType':'ORDER', 'rateSearchParam':{'sellerRateStatus':'5'}},
        owner_id=owner_id, origin='https://seller.goofish.com', referer='https://seller.goofish.com/?site=COMMONPRO')
    if not response.get('success'):
        return failed(response.get('error') or 'rate_list_unverified', response)
    raw = response.get('res') or {}
    data = raw.get('data') or {}
    module = data.get('module') if isinstance(data, dict) else None
    if not isinstance(module, dict) or not isinstance(module.get('items'), list):
        return failed('rate_list_schema_error')
    try:
        total = int(module['totalCount'])
        if total < 0 or isinstance(module['totalCount'], bool):
            raise ValueError('invalid_count')
    except (KeyError, TypeError, ValueError):
        return failed('rate_list_schema_error')
    return {'success':True, 'items':module['items'], 'total_count':total,
            'message':'获取成功', 'cookies_str':cookie_string}


async def get_rate_feedback_content(account_id: str) -> Optional[str]:
    """根据账号配置获取评价内容
    
    Args:
        account_id: 账号ID
        
    Returns:
        评价内容，如果未启用或获取失败返回None
    """
    try:
        from common.db.session import async_session_maker
        from common.models.auto_rate_config import AutoRateConfig
        from sqlalchemy import select
        
        async with async_session_maker() as session:
            stmt = select(AutoRateConfig).where(AutoRateConfig.account_id == account_id)
            result = await session.execute(stmt)
            config = result.scalars().first()
            
            if not config or not config.enabled:
                logger.info(f"账号 {account_id} 未启用自动评价")
                return None
            
            if config.rate_type == "text":
                # 固定文字
                content = config.text_content or "不错的买家"
                logger.info(f"账号 {account_id} 使用固定评价内容: {content}")
                return content
            elif config.rate_type == "api":
                # API获取
                if not config.api_url:
                    logger.warning(f"账号 {account_id} 未配置API地址")
                    return None
                
                logger.info(f"账号 {account_id} 从API获取评价内容: {config.api_url}")
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as http_session:
                    async with http_session.get(config.api_url) as response:
                        if response.status == 200:
                            content = await response.text()
                            content = content.strip()
                            if content:
                                logger.info(f"账号 {account_id} API返回评价内容: {content[:50]}...")
                                return content
                            else:
                                logger.warning(f"账号 {account_id} API返回内容为空")
                                return None
                        else:
                            logger.warning(f"账号 {account_id} API请求失败: status={response.status}")
                            return None
            else:
                logger.warning(f"账号 {account_id} 未知的评价类型: {config.rate_type}")
                return None
                
    except Exception as e:
        logger.error(f"获取评价内容失败: account_id={account_id}, error={e}")
        return None


async def update_order_rated_status(order_no: str, is_rated: bool = True) -> bool:
    """更新订单评价状态
    
    Args:
        order_no: 订单号
        is_rated: 是否已评价
        
    Returns:
        是否更新成功
    """
    try:
        from common.db.session import async_session_maker
        from common.models.xy_order import XYOrder
        from sqlalchemy import update
        
        async with async_session_maker() as session:
            stmt = update(XYOrder).where(XYOrder.order_no == order_no).values(is_rated=is_rated)
            result = await session.execute(stmt)
            await session.commit()
            
            if result.rowcount > 0:
                logger.info(f"订单 {order_no} 评价状态已更新为: {is_rated}")
                return True
            else:
                logger.warning(f"订单 {order_no} 不存在，无法更新评价状态")
                return False
                
    except Exception as e:
        logger.error(f"更新订单评价状态失败: order_no={order_no}, error={e}")
        return False


async def get_thanks_message_content(account_id: str) -> Optional[str]:
    """根据账号配置获取"好评后自动发送消息"的内容（#232）

    Args:
        account_id: 账号ID

    Returns:
        消息内容；未启用好评后消息、内容为空或获取失败时返回None
    """
    try:
        from common.db.session import async_session_maker
        from common.models.auto_rate_config import AutoRateConfig
        from sqlalchemy import select

        async with async_session_maker() as session:
            stmt = select(AutoRateConfig).where(AutoRateConfig.account_id == account_id)
            result = await session.execute(stmt)
            config = result.scalars().first()

            if not config or not config.thanks_enabled:
                return None

            content = (config.thanks_content or "").strip()
            if not content:
                logger.warning(f"账号 {account_id} 已启用好评后消息，但消息内容为空，跳过发送")
                return None
            return content

    except Exception as e:
        logger.error(f"获取好评后消息配置失败: account_id={account_id}, error={e}")
        return None


async def get_order_buyer_id(order_no: str) -> Optional[str]:
    """查询订单的买家ID（好评后消息收件人解析用，#232）

    评价请求为系统卡片消息，其 send_user_id 不一定是买家；
    发送消息前优先用订单表中的买家ID作为收件人。

    Args:
        order_no: 订单号

    Returns:
        买家ID；订单不存在或查询失败返回None
    """
    try:
        from common.db.session import async_session_maker
        from common.models.xy_order import XYOrder
        from sqlalchemy import select

        async with async_session_maker() as session:
            stmt = select(XYOrder.buyer_id).where(XYOrder.order_no == order_no)
            result = await session.execute(stmt)
            buyer_id = result.scalar_one_or_none()
            return buyer_id or None
    except Exception as e:
        logger.error(f"查询订单买家ID失败: order_no={order_no}, error={e}")
        return None


async def is_order_thanks_sent(order_no: str) -> bool:
    """检查订单是否已发送过好评后消息（防重复发送）

    Args:
        order_no: 订单号

    Returns:
        True表示已发送过；订单不存在或查询失败返回False
    """
    try:
        from common.db.session import async_session_maker
        from common.models.xy_order import XYOrder
        from sqlalchemy import select

        async with async_session_maker() as session:
            stmt = select(XYOrder.is_thanks_sent).where(XYOrder.order_no == order_no)
            result = await session.execute(stmt)
            return bool(result.scalar_one_or_none())
    except Exception as e:
        logger.error(f"查询好评后消息发送状态失败: order_no={order_no}, error={e}")
        return False


async def mark_order_thanks_sent(order_no: str) -> bool:
    """标记订单已发送好评后消息

    Args:
        order_no: 订单号

    Returns:
        是否更新成功
    """
    try:
        from common.db.session import async_session_maker
        from common.models.xy_order import XYOrder
        from sqlalchemy import update

        async with async_session_maker() as session:
            stmt = update(XYOrder).where(XYOrder.order_no == order_no).values(is_thanks_sent=True)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0
    except Exception as e:
        logger.error(f"标记好评后消息发送状态失败: order_no={order_no}, error={e}")
        return False


async def check_item_belongs_to_account(account_id: str, item_id: str) -> bool:
    """检查商品是否属于指定账号
    
    通过查询xy_catalog_items表，判断商品是否属于当前账号
    
    Args:
        account_id: 账号ID（cookie_id）
        item_id: 商品ID
        
    Returns:
        True表示商品属于该账号，False表示不属于
    """
    if not account_id or not item_id:
        logger.warning(f"检查商品归属失败: account_id={account_id}, item_id={item_id} 参数为空")
        return False
    
    try:
        from common.db.session import async_session_maker
        from common.models.xy_catalog_item import XYCatalogItem
        from common.models.xy_account import XYAccount
        from sqlalchemy import select
        
        async with async_session_maker() as session:
            # 先查询账号的主键ID
            account_stmt = select(XYAccount.id).where(XYAccount.account_id == account_id)
            account_result = await session.execute(account_stmt)
            account_pk = account_result.scalar_one_or_none()
            
            if not account_pk:
                logger.warning(f"检查商品归属: 账号 {account_id} 不存在")
                return False
            
            # 查询商品是否属于该账号
            item_stmt = select(XYCatalogItem).where(
                XYCatalogItem.account_pk == account_pk,
                XYCatalogItem.item_id == item_id
            )
            item_result = await session.execute(item_stmt)
            item = item_result.scalars().first()
            
            if item:
                logger.debug(f"商品 {item_id} 属于账号 {account_id}")
                return True
            else:
                logger.info(f"商品 {item_id} 不属于账号 {account_id}，跳过评价")
                return False
                
    except Exception as e:
        logger.error(f"检查商品归属失败: account_id={account_id}, item_id={item_id}, error={e}")
        return False
