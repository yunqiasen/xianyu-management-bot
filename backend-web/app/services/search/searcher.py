"""
商品搜索服务

基于Playwright实现闲鱼商品搜索
复刻原始 utils/item_search.py 的逻辑
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from loguru import logger
from common.services.product_results import product_error_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.xy_account import XYAccount
from app.services.search.browser import BrowserManager, PLAYWRIGHT_AVAILABLE
from app.services.search.parser import ItemParser
from app.services.search.slider_handler import SliderHandler


class ItemSearchService:
    """商品搜索服务"""

    # 搜索相关选择器
    SEARCH_INPUT_SELECTORS = [
        'input[class*="search-input"]',
        'input[placeholder*="搜索"]',
        'input[type="text"]',
        '.search-input',
        '#search-input'
    ]

    NEXT_PAGE_SELECTORS = [
        '.search-page-tiny-arrow-right--oXVFaRao',
        '[class*="search-page-tiny-arrow-right"]',
        'button[aria-label="下一页"]',
        'button:has-text("下一页")',
        'a:has-text("下一页")',
        '.ant-pagination-next',
        'li.ant-pagination-next a',
        'a[aria-label="下一页"]'
    ]

    def __init__(self, db_session: Optional[AsyncSession] = None, user_id: str = "default", account_id: Optional[str] = None):
        """
        初始化搜索服务
        
        Args:
            db_session: 异步数据库会话（可选，用于获取Cookie）
            user_id: 用户ID，用于滑块验证会话
        """
        self.db_session = db_session
        self.user_id = str(user_id)
        self.account_id = account_id
        self.response_error = None
        self.browser = BrowserManager()
        self.parser = ItemParser()
        self.slider_handler = SliderHandler(user_id)
        self.api_responses: List[Dict] = []
        self.data_list: List[Dict] = []

    async def get_first_valid_cookie(self) -> Optional[Dict[str, str]]:
        """获取第一个有效的cookie"""
        if not self.db_session or not str(self.user_id).isdigit():
            logger.error("数据库会话未初始化")
            return None

        try:
            conditions = [XYAccount.status == "active"]
            # 尽量只使用当前登录用户自己的账号 Cookie（避免跨用户取到别人的 Cookie）
            if isinstance(self.user_id, str) and self.user_id.isdigit():
                conditions.append(XYAccount.owner_id == int(self.user_id))

            if self.account_id:
                conditions.append(XYAccount.account_id == self.account_id)
            stmt = select(XYAccount).where(*conditions).order_by(XYAccount.id).limit(1)
            result = await self.db_session.execute(stmt)
            account = result.scalars().first()

            if account and account.cookie :
                logger.info(f"找到有效cookie: {account.account_id}")
                return {
                    'id': account.account_id,
                    'value': account.cookie,
                    'account': account
                }

            return None

        except Exception as e:
            logger.error(f"获取cookie失败: {str(e)}")
            return None

    async def _on_response(self, response):
        if "h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search" not in response.url:
            return
        try:
            if response.status != 200:
                status = "rate_limited" if response.status == 429 else "transport_error"
                self.response_error = {"error": f"搜索HTTP状态 {response.status}", "status": status}
                return
            payload = await response.json()
            self.api_responses.append(payload)
            ret = payload.get("ret")
            if ret and not any(str(v).startswith("SUCCESS") for v in ret):
                message = str(ret[0])
                self.response_error = {"error": message, "status": product_error_status(message)}
                return
            raw = payload.get("data", {}).get("resultList")
            if not isinstance(raw, list):
                self.response_error = {"error": "搜索响应结构错误", "status": "schema_error"}
                return
            self.data_list.extend(await self.parser.parse_items_batch(raw))
        except Exception:
            self.response_error = {"error": "搜索响应结构错误", "status": "schema_error"}

    async def search_items(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20
    ) -> Dict[str, Any]:
        """Search with the account executor; browsing never creates a second login."""
        from common.services.xianyu_search_client import XianyuSearchClient
        from common.services.product_admission import product_admission
        from common.services.product_results import product_retry_hint
        if not isinstance(keyword,str) or not keyword.strip() or page < 1 or not 1 <= page_size <= 100:
            return {'items':[], 'total':0, 'error':'搜索参数无效', 'status':'schema_error'}
        cookie_data = await self.get_first_valid_cookie()
        if not cookie_data:
            return {'items':[], 'total':0, 'error':'未选择可用账号', 'status':'account_unavailable'}
        account = cookie_data['account']
        admission = product_admission(account)
        if not admission['allowed']:
            return {**admission, 'items':[], 'total':0, 'error':admission['message']}
        client = XianyuSearchClient(account.account_id, account.cookie, owner_id=account.owner_id)
        result = await client.search(keyword.strip(), page_number=page, rows_per_page=page_size)
        if not result.get('success'):
            error = result.get('error') or '搜索结果待核实'
            status = product_error_status(error)
            return {'items':[], 'total':0, 'error':error, 'status':status,
                    **product_retry_hint(status,result)}
        items = await self.parser.parse_items_batch(result['items'])
        return {'items':self.parser.sort_by_want_count(items), 'total':len(items),
                'has_more':result['has_next_page'], 'page':page, 'is_real_data':True,
                'source':'account_executor'}

    async def search_multiple_pages(self, keyword: str, total_pages: int = 1,
                                    page_size: int = 20, start_page: int = 1) -> Dict[str, Any]:
        if not 1 <= total_pages <= 20 or not 1 <= page_size <= 100 or start_page < 1:
            return {'items': [], 'total': 0, 'error': '分页范围错误', 'status': 'invalid_input'}
        items, seen = [], set()
        for page in range(start_page, start_page + total_pages):
            result = await self.search_items(keyword, page, page_size)
            for item in result.get('items', []):
                key = str(item.get('item_id') or item.get('id') or '')
                if key and key not in seen:
                    seen.add(key)
                    items.append(item)
            if result.get('error'):
                from common.services.product_results import product_retry_hint
                return {**result, **product_retry_hint(result.get('status'), result), 'items': items, 'total': len(items), 'partial': bool(items), 'failed_page': page}
            if not result.get('items'):
                break
        return {'items': items, 'total': len(items), 'status': 'complete' if items else 'empty'}

    async def _find_search_input(self):
        """查找搜索输入框"""
        if not self.browser.page:
            return None

        for selector in self.SEARCH_INPUT_SELECTORS:
            try:
                element = await self.browser.page.wait_for_selector(selector, timeout=5000)
                if element:
                    logger.info(f"✅ 找到搜索框: {selector}")
                    return element
            except Exception:
                continue

        return None

    async def _navigate_to_page(self, target_page: int):
        """导航到指定页面"""
        try:
            logger.info(f"正在导航到第 {target_page} 页...")
            await asyncio.sleep(2)

            for current_page in range(2, target_page + 1):
                success = await self._click_next_page(current_page)
                if not success:
                    return False
            return True
        except Exception as e:
            logger.error(f"导航失败: {str(e)}")
            return False

    async def _click_next_page(self, page_num: int) -> bool:
        """点击下一页"""
        if not self.browser.page:
            return False

        logger.info(f"正在获取第 {page_num} 页...")
        await asyncio.sleep(2)

        before_count = len(self.data_list)

        for selector in self.NEXT_PAGE_SELECTORS:
            try:
                next_button = self.browser.page.locator(selector).first

                if await next_button.is_visible(timeout=3000):
                    is_disabled = await next_button.get_attribute("disabled")
                    has_disabled_class = await next_button.evaluate(
                        "el => el.classList.contains('ant-pagination-disabled') || el.classList.contains('disabled')"
                    )

                    if not is_disabled and not has_disabled_class:
                        await next_button.scroll_into_view_if_needed()
                        await asyncio.sleep(1)
                        await next_button.click()
                        await self.browser.wait_for_network_idle(timeout=15000)
                        await asyncio.sleep(5)

                        after_count = len(self.data_list)
                        new_items = after_count - before_count

                        if new_items > 0:
                            logger.info(f"第 {page_num} 页成功，新增 {new_items} 条数据")
                            return True
                        else:
                            logger.warning(f"第 {page_num} 页没有新数据")
                            return False

            except Exception:
                continue

        logger.warning(f"无法找到下一页按钮")
        return False

    def _format_error_message(self, error_msg: str) -> str:
        """格式化错误信息"""
        if "Executable doesn't exist" in error_msg or "playwright install" in error_msg:
            return "浏览器未安装。请运行: playwright install chromium"
        elif "BrowserType.launch" in error_msg:
            return "浏览器启动失败"
        elif "Target page, context or browser has been closed" in error_msg:
            return "浏览器页面被意外关闭"
        elif "Timeout" in error_msg:
            return "页面加载超时"
        return error_msg


# Each request spends at most one attempt per page; account recovery owns retries.
async def search_xianyu_items(keyword: str, page: int = 1, page_size: int = 20,
                             db_session: Optional[AsyncSession] = None, user_id: str = "default",
                             account_id: Optional[str] = None) -> Dict[str, Any]:
    service = ItemSearchService(db_session, user_id=user_id, account_id=account_id)
    return await service.search_items(keyword, page, page_size)


async def search_multiple_pages_xianyu(keyword: str, total_pages: int = 1,
                                      db_session: Optional[AsyncSession] = None,
                                      user_id: str = "default", account_id: Optional[str] = None) -> Dict[str, Any]:
    service = ItemSearchService(db_session, user_id=user_id, account_id=account_id)
    return await service.search_multiple_pages(keyword, total_pages)
