"""
定时擦亮任务服务

功能：
1. 定时检查开启了商品自动擦亮的账号
2. 查询该账号下未擦亮的商品
3. 调用闲鱼API执行商品擦亮
4. 更新商品擦亮状态
5. 记录执行日志
"""
import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

import aiohttp
from loguru import logger
from sqlalchemy import delete as sql_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.db.session import async_session_maker
from common.models.xy_account import XYAccount
from common.models.xy_catalog_item import XYCatalogItem
from common.models.scheduled_polish_log import ScheduledPolishLog
from common.utils.xianyu_utils import trans_cookies, generate_sign
from common.utils.cookie_refresh import update_account_cookies_in_db
from common.utils.time_utils import get_beijing_now_naive


class PolishTaskService:
    """定时擦亮任务服务"""

    # 擦亮日志保留天数，超过该天数的日志在每次任务执行时主动清理
    LOG_RETENTION_DAYS = 10

    def __init__(self):
        self.task_name = "定时擦亮"

    async def execute(self):
        """执行定时擦亮任务"""
        logger.info(f"【{self.task_name}】开始执行")
        start_time = datetime.now()
        
        # 生成批次ID
        batch_id = str(uuid.uuid4())

        try:
            async with async_session_maker() as session:
                # 主动清理过期的擦亮日志（10天前）
                await self._cleanup_expired_logs(session)

                # 1. 查询开启了商品自动擦亮的账号
                accounts = await self._get_enabled_accounts(session)
                
                if not accounts:
                    logger.info(f"【{self.task_name}】没有开启商品自动擦亮的账号")
                    return

                logger.info(f"【{self.task_name}】找到 {len(accounts)} 个开启商品自动擦亮的账号")

                # 2. 遍历账号，处理每个账号的商品
                total_success = 0
                total_failed = 0
                
                for account in accounts:
                    # 检查账号是否处于Session过期冷却期内
                    from common.utils.cookie_refresh import is_account_session_cooled
                    if is_account_session_cooled(account.account_id):
                        logger.info(
                            f"【{self.task_name}】账号 {account.account_id} "
                            f"处于Session过期冷却期内，跳过"
                        )
                        continue
                    
                    try:
                        success, failed = await self._process_account(session, account, batch_id)
                        total_success += success
                        total_failed += failed
                    except Exception as e:
                        logger.error(f"【{self.task_name}】处理账号 {account.account_id} 失败: {e}")
                        total_failed += 1

                # 3. 记录执行结果
                elapsed = (datetime.now() - start_time).total_seconds()
                logger.info(
                    f"【{self.task_name}】执行完成，批次ID: {batch_id}, "
                    f"成功: {total_success}, 失败: {total_failed}, "
                    f"耗时: {elapsed:.2f}秒"
                )

        except Exception as e:
            logger.error(f"【{self.task_name}】执行失败: {e}")
            raise

    async def _cleanup_expired_logs(self, session: AsyncSession) -> None:
        """
        主动清理过期的擦亮日志

        删除 created_at 早于 (当前北京时间 - LOG_RETENTION_DAYS 天) 的日志记录，
        避免日志表无限增长。使用参数化的 ORM delete 语句，避免 SQL 注入。
        """
        try:
            cutoff_time = get_beijing_now_naive() - timedelta(days=self.LOG_RETENTION_DAYS)
            stmt = sql_delete(ScheduledPolishLog).where(
                ScheduledPolishLog.created_at < cutoff_time
            )
            result = await session.execute(stmt)
            await session.commit()

            deleted_count = result.rowcount or 0
            if deleted_count > 0:
                logger.info(
                    f"【{self.task_name}】已清理 {deleted_count} 条 {self.LOG_RETENTION_DAYS} 天前的擦亮日志"
                    f"（清理时间界限: {cutoff_time}）"
                )
        except Exception as e:
            logger.error(f"【{self.task_name}】清理过期日志失败: {e}")
            await session.rollback()

    async def _get_enabled_accounts(self, session: AsyncSession) -> List[XYAccount]:
        """
        获取开启了商品自动擦亮的账号
        
        条件：
        - status = 'active'（启用）
        - auto_polish = True（商品自动擦亮开启）
        """
        stmt = select(XYAccount).where(
            XYAccount.status == "active",
            XYAccount.auto_polish == True,
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _process_account(self, session: AsyncSession, account: XYAccount, batch_id: str) -> tuple[int, int]:
        """
        处理单个账号的商品擦亮
        
        Args:
            session: 数据库会话
            account: 账号对象
            batch_id: 批次ID
        
        Returns:
            (成功数量, 失败数量)
        """
        from common.services.product_admission import product_admission
        state = product_admission(account)
        if not state['allowed']:
            await self._log_execution(session, batch_id, account.account_id, '-', False,
                                      f"skipped:{state['status']}; next={state.get('next_retry_at', '-')}" )
            return 0, 0
        from common.services.product_polish_service import ProductPolishService
        result = await ProductPolishService(session).run(account, source='scheduled', batch_id=batch_id)
        entries = result.get('items', [])
        return sum(r['status'] == 'success' for r in entries), sum(r['status'] in ('failed', 'unknown') for r in entries)

    async def _get_unpolished_items(self, session: AsyncSession, account_pk: int) -> List[XYCatalogItem]:
        """
        获取账号下未擦亮的商品
        
        条件：
        - account_pk = 指定账号ID
        - is_polished = False 或 NULL（未擦亮）
        """
        stmt = select(XYCatalogItem).where(
            XYCatalogItem.account_pk == account_pk,
            (XYCatalogItem.is_polished == False) | (XYCatalogItem.is_polished == None),
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _polish_item(self, cookie_str: str, item_id: str, retry_count: int = 0) -> dict:
        """
        擦亮商品
        
        支持令牌过期时存储set-cookie并重试（参照发货服务模式）
        
        Args:
            cookie_str: Cookie字符串
            item_id: 商品ID
            retry_count: 当前重试次数
            
        Returns:
            {"success": bool, "message": str}
        """
        max_retry = 3
        
        try:
            # 解析Cookie
            cookies = trans_cookies(cookie_str)
            
            # 获取token
            token = cookies.get('_m_h5_tk', '').split('_')[0] if cookies.get('_m_h5_tk') else ''
            
            if not token:
                return {"success": False, "message": "Cookie中没有找到_m_h5_tk"}
            
            # 生成时间戳
            t = str(int(time.time() * 1000))
            
            # 构造请求参数
            params = {
                'jsv': '2.7.2',
                'appKey': '34839810',
                't': t,
                'sign': '',
                'v': '2.0',
                'type': 'originaljson',
                'accountSite': 'xianyu',
                'dataType': 'json',
                'timeout': '20000',
                'api': 'mtop.taobao.idle.item.polish',
                'sessionOption': 'AutoLoginOnly',
                'spm_cnt': 'a21ybx.item.0.0',
                'spm_pre': 'a21ybx.personal.feeds.1.42f86ac21eZ9zd',
                'log_id': '42f86ac21eZ9zd'
            }
            
            # 构造请求数据
            data = {
                'itemId': item_id
            }
            
            # 生成签名
            data_val = json.dumps(data, separators=(',', ':'))
            sign = generate_sign(params['t'], token, data_val)
            params['sign'] = sign
            
            # 构造请求头
            headers = {
                'accept': 'application/json',
                'accept-language': 'en,zh-CN;q=0.9,zh;q=0.8,ru;q=0.7',
                'cache-control': 'no-cache',
                'content-type': 'application/x-www-form-urlencoded',
                'pragma': 'no-cache',
                'priority': 'u=1, i',
                'sec-ch-ua': '"Google Chrome";v="141", "Not=A?Brand";v="8", "Not A(Brand)";v="141"',
                'sec-ch-ua-arch': '"x64"',
                'sec-ch-ua-bitness': '"64"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Win32"',
                'sec-ch-ua-platform-version': '"10.0.0"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-site',
                'cookie': cookie_str,
                'Referer': 'https://www.goofish.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            
            # 发送请求
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    'https://h5api.m.goofish.com/h5/mtop.taobao.idle.item.polish/1.0/',
                    params=params,
                    data={'data': data_val},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    result = await response.json()
                    
                    # 打印接口返回值
                    logger.info(
                        f"擦亮商品接口返回: item_id={item_id}, retry_count={retry_count}, result={result}"
                    )
                    
                    # 处理响应中的set-cookie，更新cookie（令牌过期时服务端会返回新cookie）
                    new_cookie_str = self._handle_polish_response_cookies(response, cookie_str)
                    
                    # 判断结果
                    if result.get('ret') and result['ret'][0] == 'SUCCESS::调用成功':
                        return {"success": True, "message": "擦亮成功", "cookie_str": new_cookie_str}
                    else:
                        error_msg = result.get('ret', ['未知错误'])[0] if result.get('ret') else '未知错误'
                        
                        # 如果返回"宝贝已经擦亮过了"，也视为成功
                        if '宝贝已经擦亮过了' in error_msg or 'IDLEITEM_POLISH_AGAIN' in error_msg:
                            return {"success": True, "message": "商品已经擦亮过了", "cookie_str": new_cookie_str}
                        
                        # 令牌过期时，用更新后的cookie重试
                        ret_list = result.get('ret', [])
                        if any('TOKEN_EXOIRED' in r or 'TOKEN_EXPIRED' in r for r in ret_list):
                            if retry_count < max_retry - 1:
                                logger.info(
                                    f"商品 {item_id} 擦亮令牌过期，"
                                    f"已更新Cookie，准备重试({retry_count + 1}/{max_retry - 1})"
                                )
                                await asyncio.sleep(0.5)
                                return await self._polish_item(new_cookie_str, item_id, retry_count + 1)
                        
                        return {"success": False, "message": error_msg, "cookie_str": new_cookie_str}
                        
        except aiohttp.ClientError as e:
            return {"success": False, "message": f"商品 {item_id} 网络请求失败: {e}"}
        except Exception as e:
            return {"success": False, "message": f"商品 {item_id} 擦亮异常: {e}"}
    
    def _handle_polish_response_cookies(self, response, original_cookie_str: str) -> str:
        """处理擦亮API响应中的set-cookie，返回更新后的cookie字符串
        
        令牌过期时服务端会在响应头中返回新的cookie（包含新的_m_h5_tk），
        存储后重试请求即可使用新的token签名
        
        Args:
            response: HTTP响应对象
            original_cookie_str: 原始Cookie字符串
            
        Returns:
            更新后的Cookie字符串
        """
        try:
            if 'set-cookie' in response.headers:
                new_cookies = {}
                for cookie in response.headers.getall('set-cookie', []):
                    if '=' in cookie:
                        name, value = cookie.split(';')[0].split('=', 1)
                        new_cookies[name.strip()] = value.strip()
                
                if new_cookies:
                    existing_cookies = trans_cookies(original_cookie_str)
                    existing_cookies.update(new_cookies)
                    updated_str = '; '.join([f"{k}={v}" for k, v in existing_cookies.items()])
                    logger.info(f"已从擦亮响应中更新Cookie（含{len(new_cookies)}个字段）")
                    return updated_str
        except Exception as e:
            logger.warning(f"处理擦亮响应Cookie失败: {e}")
        
        return original_cookie_str
    
    async def _log_execution(
        self,
        session: AsyncSession,
        batch_id: str,
        account_id: str,
        item_id: str,
        success: bool,
        error_message: Optional[str] = None,
    ) -> None:
        """
        记录执行日志
        
        Args:
            session: 数据库会话
            batch_id: 批次ID
            account_id: 账号ID
            item_id: 商品ID
            success: 是否成功
            error_message: 错误信息
        """
        try:
            log = ScheduledPolishLog(
                batch_id=batch_id,
                account_id=account_id,
                item_id=item_id,
                status="skipped" if error_message and error_message.startswith("skipped:") else ("success" if success else "failed"),
                error_message=error_message[:500] if error_message else None,
            )
            session.add(log)
            await session.commit()
            
            # 只有成功时才打印INFO日志，失败时打印DEBUG日志
            if success:
                logger.info(f"【{self.task_name}】账号 {account_id} 商品 {item_id} 处理成功")
            else:
                logger.debug(f"【{self.task_name}】账号 {account_id} 商品 {item_id} 处理失败: {error_message}")
            
        except Exception as e:
            logger.error(f"【{self.task_name}】记录日志失败: {e}")
            await session.rollback()


# 创建全局实例
polish_task_service = PolishTaskService()
