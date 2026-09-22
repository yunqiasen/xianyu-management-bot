"""
商品信息管理模块

提供商品信息的获取、保存等功能（不依赖 WebSocket）
"""
import asyncio
import json
import time
from typing import Optional, Dict, Any, List

from loguru import logger

from common.utils.text_utils import safe_str


class ItemInfoManager:
    """商品信息管理器
    
    管理商品信息的获取、保存等操作（纯 HTTP API 调用，不需要 WebSocket）
    """
    
    def __init__(self, cookie_id: str, cookies_str: str, session=None, *, owner_id: int | None = None):
        """初始化商品信息管理器
        
        Args:
            cookie_id: 账号ID
            cookies_str: Cookie字符串
            session: aiohttp session（可选）
        """
        self.owner_id = owner_id
        self.cookie_id = cookie_id
        self.cookies_str = cookies_str
        self.cookies = self._parse_cookies(cookies_str)
        self.session = session
        self._own_session = False
    
    def _parse_cookies(self, cookies_str: str) -> dict:
        """解析Cookie字符串为字典"""
        from common.utils.xianyu_utils import trans_cookies
        return trans_cookies(cookies_str)
    
    def _safe_str(self, e) -> str:
        """安全地将异常转换为字符串（委托公共实现）"""
        return safe_str(e)
    
    def update_cookies(self, cookies_str: str):
        """更新Cookie"""
        self.cookies_str = cookies_str
        self.cookies = self._parse_cookies(cookies_str)
    
    async def close(self):
        """关闭session"""
        if self._own_session and self.session:
            await self.session.close()
            self.session = None
            self._own_session = False

    async def get_item_list_info(self, page_number=1, page_size=20, retry_count=0,
                                 update_config_cookies_callback=None, myid=None):
        """One page via the account executor; no local session, credential writes or retry."""
        from common.services.xianyu_mtop import mtop_call
        from common.services.account_dispatch import CURRENT_OPERATION
        context = CURRENT_OPERATION.get()
        owner = self.owner_id
        if owner is None and context is not None and context.request.account_id == self.cookie_id:
            owner = context.request.owner_id
        if owner is None or page_number < 1 or not 1 <= page_size <= 100:
            return {'success':False, 'error':'catalog_identity_or_page_invalid'}
        response = await mtop_call(self.cookie_id, self.cookies_str, 'mtop.idle.web.xyh.item.list',
            '1.0', {'needGroupInfo':False, 'pageNumber':page_number, 'pageSize':page_size,
                    'groupName':'在售', 'groupId':'58877261', 'defaultGroup':True,
                    'userId':myid or self.cookie_id}, owner_id=owner)
        if not response.get('success'):
            return {'success':False, 'error':response.get('error','catalog_request_failed'),
                    'message':response.get('error','catalog_request_failed')}
        body = (response.get('res') or {}).get('data')
        if not isinstance(body, dict) or not isinstance(body.get('cardList'), list):
            return {'success':False, 'error':'catalog_schema_error', 'message':'catalog_schema_error'}
        items = []
        for card in body['cardList']:
            data = card.get('cardData') if isinstance(card,dict) else None
            if not isinstance(data,dict) or not data.get('id'):
                return {'success':False, 'error':'catalog_schema_error', 'message':'catalog_schema_error'}
            price = data.get('priceInfo') or {}
            if not isinstance(price,dict):
                return {'success':False, 'error':'catalog_schema_error', 'message':'catalog_schema_error'}
            items.append({'id':str(data['id']), 'title':data.get('title',''),
                'price':price.get('price',''), 'price_text':str(price.get('preText',''))+str(price.get('price','')),
                'category_id':data.get('categoryId',''), 'auction_type':data.get('auctionType',''),
                'item_status':data.get('itemStatus',0), 'detail_url':data.get('detailUrl',''),
                'pic_info':data.get('picInfo',{}), 'detail_params':data.get('detailParams',{}),
                'track_params':data.get('trackParams',{}), 'item_label_data':data.get('itemLabelDataVO',{}),
                'card_type':card.get('cardType',0)})
        return {'success':True, 'items':items, 'page_number':page_number, 'page_size':page_size,
                'current_count':len(items), 'raw_data':body}

    async def get_all_items(self, page_size=20, max_pages=None, update_config_cookies_callback=None, myid=None):
        """获取所有商品信息（自动分页）

        Args:
            page_size (int): 每页数量，默认20
            max_pages (int): 最大页数限制，None表示无限制
            update_config_cookies_callback: 更新Cookie的回调函数
            myid: 用户ID

        Returns:
            dict: 包含所有商品信息的字典
        """
        all_items = []
        seen = set()
        page_number = 1

        logger.info(f"开始获取所有商品信息，每页{page_size}条")

        while True:
            if max_pages and page_number > max_pages:
                logger.info(f"达到最大页数限制 {max_pages}，停止获取")
                break

            logger.info(f"正在获取第 {page_number} 页...")
            result = await self.get_item_list_info(page_number, page_size, 0, update_config_cookies_callback, myid)

            if not result.get("success"):
                error = result.get('error') or 'catalog_request_failed'
                logger.warning("商品同步在第 {} 页停止: {}", page_number, error)
                return {
                    'success': False, 'partial': bool(all_items), 'complete': False,
                    'failed_page': page_number, 'total_pages': page_number - 1,
                    'total_count': len(all_items), 'items': all_items,
                    'error': error, 'retry_after': result.get('retry_after'),
                }

            current_items = result.get("items", [])
            if not current_items:
                logger.info(f"第 {page_number} 页没有数据，获取完成")
                break

            fresh_items = []
            for item in current_items:
                if item['id'] not in seen:
                    seen.add(item['id'])
                    fresh_items.append(item)
            if not fresh_items and len(current_items) >= page_size:
                return {'success': False, 'partial': bool(all_items), 'complete': False,
                        'error': 'catalog_pagination_stalled', 'failed_page': page_number,
                        'total_pages': page_number - 1, 'total_count': len(all_items), 'items': all_items}
            all_items.extend(fresh_items)

            logger.info(f"第 {page_number} 页获取到 {len(current_items)} 个商品")

            if len(current_items) < page_size:
                logger.info(f"第 {page_number} 页商品数量少于页面大小，获取完成")
                break

            page_number += 1
            await asyncio.sleep(1)

        logger.info(f"所有商品获取完成，共 {len(all_items)} 个商品")

        return {
            "success": True,
            "total_pages": page_number,
            "total_count": len(all_items),
            "items": all_items
        }
