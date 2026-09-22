"""
Cookie/Token管理模块
负责处理Cookie和Token相关的所有逻辑，包括：
- Token刷新
- 滑块验证检测和处理
- Cookie更新和重启
- 密码登录刷新
- Cookie有效性验证
- 浏览器刷新Cookie
- 扫码登录Cookie刷新
"""

import asyncio
import json
import random
import time
import aiohttp
from typing import Optional
from loguru import logger
from sqlalchemy import select

from common.db.session import async_session_maker
from common.models.system_setting import SystemSetting
from common.models.token_cache import TokenCache
from common.services.im_token_api import (
    extract_im_access_token,
    is_session_expired_token_result,
    request_im_token_with_fallback,
)
from common.services.risk_control_log_query_service import (
    check_account_processing_risk_control_log,
    get_account_risk_control_lock,
)
from common.services.token_renewal_cache_service import delete_token_cache
from common.services.token_api_mode import (
    get_token_api_mode_label,
    load_token_api_mode,
)
from common.services.captcha.concurrency import run_browser_task
from common.services.captcha.slider_mode import (
    SLIDER_MODE_REAL_MOUSE,
    refresh_slider_mode_from_database,
)
from common.services.captcha.token_refetch import request_fresh_captcha_url
from common.services.captcha.token_response import (
    get_token_captcha_reason,
    is_token_expired_response,
)
from common.services.captcha.weighted_runner import real_mouse_weighted_runner
from common.utils.cookie_refresh import get_account_by_identity, update_account_cookies_in_db
from common.utils.xianyu_utils import trans_cookies
from common.utils.time_utils import get_beijing_now_naive, random_token_cache_expiry
from common.utils.token_cache import TokenCacheValidity, classify_token_cache_validity


STARTUP_EXPIRED_CACHE_REFRESH_JITTER_SECONDS = 120

class CookieTokenManager:
    """Cookie/Token管理器"""
    
    def __init__(self, parent):
        """
        初始化Cookie/Token管理器
        
        Args:
            parent: XianyuLive实例，用于访问共享资源
        """
        self.parent = parent
        self._startup_expired_cache_available = True
        self._last_cache_lookup_succeeded = True
        self._cached_token_in_use: str | None = None
    
    # ==================== 属性代理 ====================
    
    @property
    def cookie_id(self):
        return self.parent.cookie_id
    
    @property
    def cookies_str(self):
        return self.parent.cookies_str
    
    @cookies_str.setter
    def cookies_str(self, value):
        self.parent.cookies_str = value
    
    @property
    def cookies(self):
        return self.parent.cookies
    
    @cookies.setter
    def cookies(self, value):
        self.parent.cookies = value
    
    @property
    def session(self):
        return self.parent.session
    
    @property
    def current_token(self):
        return self.parent.current_token
    
    @current_token.setter
    def current_token(self, value):
        self.parent.current_token = value
    
    @property
    def last_token_refresh_time(self):
        return self.parent.last_token_refresh_time
    
    @last_token_refresh_time.setter
    def last_token_refresh_time(self, value):
        self.parent.last_token_refresh_time = value
    
    @property
    def last_token_refresh_status(self):
        return self.parent.last_token_refresh_status
    
    @last_token_refresh_status.setter
    def last_token_refresh_status(self, value):
        self.parent.last_token_refresh_status = value
    
    @property
    def device_id(self):
        return self.parent.device_id

    @device_id.setter
    def device_id(self, value):
        self.parent.device_id = value

    @property
    def myid(self):
        return self.parent.myid
    
    @property
    def max_captcha_verification_count(self):
        return self.parent.max_captcha_verification_count
    
    @property
    def last_message_received_time(self):
        return self.parent.last_message_received_time
    
    @property
    def message_cookie_refresh_cooldown(self):
        return self.parent.message_cookie_refresh_cooldown
    
    @property
    def restarted_in_browser_refresh(self):
        return self.parent.restarted_in_browser_refresh
    
    @restarted_in_browser_refresh.setter
    def restarted_in_browser_refresh(self, value):
        self.parent.restarted_in_browser_refresh = value
    
    # ==================== 辅助方法代理 ====================
    
    def _safe_str(self, obj):
        return self.parent._safe_str(obj)
    
    async def create_session(self):
        return await self.parent.create_session()
    
    async def send_token_refresh_notification(self, error_message: str, notification_type: str = "token_refresh",
                                             chat_id: str = None, attachment_path: str = None,
                                             verification_url: str = None):
        return await self.parent.send_token_refresh_notification(
            error_message=error_message,
            notification_type=notification_type,
            chat_id=chat_id,
            attachment_path=attachment_path,
            verification_url=verification_url
        )
    
    async def restart_instance(self, reason=None):
        return await self.parent.restart_instance(reason)

    async def _load_account_record(self):
        async with async_session_maker() as session:
            return await get_account_by_identity(
                self.cookie_id,
                owner_id=getattr(self.parent, 'user_id', None),
                session=session,
            )

    async def _load_account_info(self):
        account = await self._load_account_record()
        if not account:
            return None
        return {
            'cookie_value': account.cookie,
            'username': account.username,
            'password': account.login_password,
            'show_browser': account.show_browser,
        }


    # ==================== Token缓存（数据库） ====================

    async def _is_local_slider_disabled(self) -> bool | None:
        """实时读取“本机滑块不处理”开关。

        Returns:
            True 表示不处理本机滑块，False 表示允许处理；读取失败返回 None，
            调用方应保持原有的安全策略，不调用 Token API。
        """
        try:
            async with async_session_maker() as session:
                value = (
                    await session.execute(
                        select(SystemSetting.value)
                        .where(SystemSetting.key == "captcha.local_slider_disabled")
                        .limit(1)
                    )
                ).scalar_one_or_none()
            return str(value or "false").strip().lower() == "true"
        except Exception as e:
            logger.warning(f"【{self.cookie_id}】读取本机滑块处理开关失败，本次禁止调用Token API: {e}")
            return None

    async def _get_processing_risk_control_skip_result(
        self,
        action_name: str,
    ) -> tuple[bool, str | None]:
        """检查同账号风控占用，并返回是否跳过及可继续使用的当前 Token。

        Args:
            action_name: 当前准备执行的动作名称，用于日志说明。
        Returns:
            ``(是否跳过, 当前Token)``；没有现有 Token 时第二项为 ``None``。
        """
        processing_check = await check_account_processing_risk_control_log(
            self.cookie_id
        )
        if not processing_check.has_processing:
            return False, None

        if processing_check.success:
            self.last_token_refresh_status = "skipped_risk_control_processing"
        else:
            self.last_token_refresh_status = "skipped_risk_control_check_failed"

        current_token = self.current_token
        if current_token:
            logger.warning(
                f"【{self.cookie_id}】{processing_check.message}，"
                f"本次跳过{action_name}，继续沿用现有WebSocket连接"
            )
        else:
            logger.warning(
                f"【{self.cookie_id}】{processing_check.message}，"
                f"本次跳过{action_name}，等待下一轮Token刷新"
            )
        return True, current_token

    async def _get_cached_token(
        self,
        allow_expired: bool = False,
        *,
        expired_cache_reason: str = "local_slider_disabled",
    ) -> dict | None:
        """从数据库获取缓存的token和device_id
        
        原到期日有效时直接返回；原到期日失效但续期到期日有效时，
        将原到期日提升为续期到期日并返回续期 Token。启用本机滑块
        不处理时，可通过 allow_expired 继续返回已过期缓存。

        Args:
            allow_expired: 是否允许返回原到期日和续期到期日均已失效的缓存。
            expired_cache_reason: 允许使用过期缓存的原因，用于状态和日志区分。
        
        Returns:
            包含 token、device_id 和是否使用续期 Token 的字典；
            不存在或两个到期日都无效时返回 None。
        """
        self._last_cache_lookup_succeeded = False
        try:
            async with async_session_maker() as session:
                cache = (
                    await session.execute(
                        select(TokenCache)
                        .where(TokenCache.user_id == self.myid)
                        .limit(1)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                self._last_cache_lookup_succeeded = True

                if cache is None:
                    logger.info(f"【{self.cookie_id}】Token缓存未命中: user_id={self.myid}")
                    return None

                if not str(cache.token or "").strip() or not str(cache.device_id or "").strip():
                    logger.warning(f"【{self.cookie_id}】Token缓存内容不完整，按未命中处理: user_id={self.myid}")
                    return None

                now = get_beijing_now_naive()
                validity = classify_token_cache_validity(
                    cache.expire_at,
                    cache.renew_expire_at,
                    now,
                )
                if validity == TokenCacheValidity.CURRENT:
                    remaining = cache.expire_at - now
                    remaining_hours = int(remaining.total_seconds() // 3600)
                    remaining_minutes = int((remaining.total_seconds() % 3600) // 60)
                    logger.info(
                        f"【{self.cookie_id}】Token缓存命中: user_id={self.myid}, "
                        f"剩余有效时间={remaining_hours}小时{remaining_minutes}分钟"
                    )
                    return {
                        "token": cache.token,
                        "device_id": cache.device_id,
                        "renewal_promoted": False,
                        "expired_fallback": False,
                    }

                if validity == TokenCacheValidity.RENEWED:
                    cache.expire_at = cache.renew_expire_at
                    await session.commit()
                    logger.info(
                        f"【{self.cookie_id}】续期Token缓存命中: user_id={self.myid}, "
                        f"原到期日已更新为 {cache.expire_at:%Y-%m-%d %H:%M:%S}"
                    )
                    return {
                        "token": cache.token,
                        "device_id": cache.device_id,
                        "renewal_promoted": True,
                        "expired_fallback": False,
                    }

                logger.info(
                    f"【{self.cookie_id}】Token缓存原到期日和续期到期日均已失效: "
                    f"user_id={self.myid}, 原到期日={cache.expire_at}, "
                    f"续期到期日={cache.renew_expire_at}"
                )
                if allow_expired:
                    reason_text = (
                        "WebSocket启动首次连接"
                        if expired_cache_reason == "websocket_startup"
                        else "本机滑块不处理已开启"
                    )
                    logger.warning(
                        f"【{self.cookie_id}】{reason_text}，暂时使用已过期Token缓存尝试连接"
                    )
                    return {
                        "token": cache.token,
                        "device_id": cache.device_id,
                        "renewal_promoted": False,
                        "expired_fallback": True,
                        "expired_fallback_reason": expired_cache_reason,
                    }
        except Exception as e:
            self._last_cache_lookup_succeeded = False
            logger.warning(f"【{self.cookie_id}】获取Token缓存失败: {e}")
        return None

    async def _use_cached_token(self, cached: dict) -> str:
        """将数据库缓存写入当前实例，并在续期 Token 生效时触发重连。

        Args:
            cached: 包含 Token、Device ID 和续期提升标记的缓存数据。

        Returns:
            已写入当前实例的 Token。
        """
        cached_token = cached["token"]
        cached_device_id = cached["device_id"]
        self.parent.device_id = cached_device_id
        self.current_token = cached_token
        self._cached_token_in_use = cached_token
        self.last_token_refresh_time = time.time()
        self.last_token_refresh_status = "success_from_cache"
        self.parent._using_expired_startup_token = False
        logger.info(f"【{self.cookie_id}】使用数据库缓存的Token和Device ID")
        logger.info(f"【{self.cookie_id}】缓存Token: {cached_token}")
        logger.info(f"【{self.cookie_id}】缓存Device ID: {cached_device_id}")
        if cached.get("expired_fallback"):
            reason = cached.get("expired_fallback_reason")
            if reason == "websocket_startup":
                self.last_token_refresh_status = "success_from_expired_startup_cache"
                self.parent._using_expired_startup_token = True
                token_manager = getattr(self.parent, "token_manager", None)
                if token_manager is not None:
                    refresh_jitter = random.uniform(
                        0,
                        STARTUP_EXPIRED_CACHE_REFRESH_JITTER_SECONDS,
                    )
                    token_manager.last_cookie_refresh_time = time.time() + refresh_jitter
                    refresh_delay = token_manager.cookie_refresh_interval + refresh_jitter
                    logger.warning(
                        f"【{self.cookie_id}】启动阶段使用过期Token缓存连接，"
                        f"将在约{refresh_delay:.0f}秒后自动刷新Token"
                    )
            else:
                self.last_token_refresh_status = "success_from_expired_cache"
        elif cached.get("renewal_promoted"):
            self.last_token_refresh_status = "success_from_renewal"
            await self._reconnect_websocket_for_renewed_token(reason="续期Token已生效")
        return cached_token

    async def _reconnect_websocket_for_renewed_token(
        self, reason: str = "Token续期生效"
    ) -> None:
        """Token 变化后关闭现有连接，由主循环携带新 Token 重连。

        闲鱼 IM 的 WebSocket 在建连时一次性认证，不支持中途换 Token。Token 变化后
        旧连接的认证已失效，必须关闭旧连接触发重连，否则会进入"心跳正常但收不到
        业务消息"的僵尸状态。

        Args:
            reason: 触发重连的原因，用于日志与关闭帧说明，便于线上排查区分
                续期生效与普通刷新两种场景。
        """
        connection_manager = getattr(self.parent, "connection_manager", None)
        websocket = getattr(connection_manager, "ws", None)
        if websocket is None or getattr(websocket, "closed", True):
            return

        logger.info(f"【{self.cookie_id}】{reason}，准备重连WebSocket")
        await websocket.close(code=1000, reason=reason)

    async def _set_cached_token(self, token: str, device_id: str):
        """将token和device_id缓存到数据库
        
        使用 INSERT ... ON DUPLICATE KEY UPDATE 实现插入或更新
        基础过期时间由环境变量 TOKEN_CACHE_TTL_MIN_HOURS / TOKEN_CACHE_TTL_MAX_HOURS 控制，
        再追加 1~5 小时的秒级随机偏移；未配置时最终 TTL 为 6~15 小时

        Args:
            token: IM Token
            device_id: 设备ID
        """
        try:
            from sqlalchemy import text

            # 基础 TTL 默认 5~10 小时，再追加 1~5 小时秒级随机偏移
            expire_at, ttl_hours = random_token_cache_expiry()
            
            async with async_session_maker() as session:
                await session.execute(
                    text("""
                        INSERT INTO xy_token_cache
                            (user_id, token, device_id, expire_at, renew_expire_at, created_at, updated_at)
                        VALUES (:user_id, :token, :device_id, :expire_at, NULL, NOW(), NOW())
                        ON DUPLICATE KEY UPDATE 
                            token = VALUES(token),
                            device_id = VALUES(device_id),
                            expire_at = VALUES(expire_at),
                            renew_expire_at = NULL,
                            updated_at = NOW()
                    """),
                    {
                        "user_id": self.myid,
                        "token": token,
                        "device_id": device_id,
                        "expire_at": expire_at
                    }
                )
                await session.commit()
                self._cached_token_in_use = token
                logger.info(f"【{self.cookie_id}】Token已缓存到数据库 (过期时间={expire_at.strftime('%Y-%m-%d %H:%M:%S')}, TTL={ttl_hours:.1f}小时)")
        except Exception as e:
            logger.warning(f"【{self.cookie_id}】缓存Token到数据库失败: {e}")

    async def _delete_cached_token(self):
        """按唯一 user_id 删除当前账号的 Token 缓存。"""
        invalidation = await delete_token_cache(
            token_user_id=self.myid,
        )
        if invalidation.success:
            logger.info(f"【{self.cookie_id}】{invalidation.message}: user_id={self.myid}")
        else:
            logger.warning(f"【{self.cookie_id}】{invalidation.message}")

    # ==================== Cookie更新 ====================

    def _clear_refetch_state(self) -> None:
        """Clear transient state produced by one captcha refetch flow."""
        self._refetch_token_ok = False
        self._refetch_new_token = None
        self._refetch_new_cookies = {}

    async def update_config_cookies(self) -> bool:
        from common.services.account_cookie_service import write_account_credentials
        runtime = getattr(self.parent, '_account_runtime', None)
        if runtime is None:
            return False
        try:
            await runtime.lease.check()
            accepted = await write_account_credentials(self.cookie_id, self.parent.user_id,
                self.cookies_str, expected=(*runtime.version, runtime.generation))
            if accepted:
                # 同一执行者自己的续签升级凭据版本；其他版本变化依旧拒绝。
                runtime.version = (runtime.version[0] + 1, runtime.version[1])
            return accepted
        except Exception:
            return False

    # ==================== 滑块验证检测 ====================

    def need_captcha_verification(self, res_json: dict) -> bool:
        """检查响应是否需要滑块验证"""
        try:
            reason = get_token_captcha_reason(res_json)
            if reason:
                logger.info(f"【{self.cookie_id}】检测到需要滑块验证: {reason}")
                return True
            return False

        except Exception as e:
            logger.error(f"【{self.cookie_id}】检查是否需要滑块验证时出错: {self._safe_str(e)}")
            return False


    # ==================== 滑块验证处理 ====================

    def _request_captcha_url_sync(self) -> Optional[str]:
        """同步重新请求 token 接口，提取一个新鲜的滑块验证链接。

        说明：滑块验证在独立线程中执行（asyncio.to_thread），而浏览器需要等待并发槽位/
        账号锁并完成启动，期间最初拿到的 punish?x5secdata 链接极易过期，导致页面显示
        "抱歉，页面访问出现了问题"。本方法在浏览器就绪后被回调，使用同步 requests 重新
        触发一次 token 接口，拿到最新的验证链接再交给浏览器导航，从源头规避链接过期。

        Returns:
            新的验证 URL；若接口已不再要求验证或解析失败则返回 None（此时沿用原链接）。
        """
        try:
            from app.services.captcha.slider_stealth import CAPTCHA_NOT_REQUIRED

            # 幂等缓存：本轮若已确认 token 可用，直接返回哨兵，绝不重复请求 token 接口。
            # 这样即便 run()/兜底编排层多次回调本方法，也只会真正请求一次，避免频繁调用。
            if getattr(self, '_refetch_token_ok', False):
                return CAPTCHA_NOT_REQUIRED

            logger.info(f"【{self.cookie_id}】浏览器就绪，重新请求新鲜的滑块验证链接...")
            # 与远程过滑块接口共用同一份"凭 Cookie 重取链接"逻辑（common.token_refetch）
            res = request_fresh_captcha_url(
                self.cookie_id, self.cookies, self.cookies_str, self.device_id
            )

            # 无论最终拿到 Token 还是新验证链接，都先把接口下发的 Cookie 合并进
            # 当前实例并累计，供后续回调继续使用，并在浏览器流程返回后统一写库。
            refetched_cookies = res.get("new_cookies")
            if isinstance(refetched_cookies, dict) and refetched_cookies:
                self.cookies.update(refetched_cookies)
                self.cookies_str = "; ".join(
                    f"{key}={value}" for key, value in self.cookies.items()
                )
                accumulated_cookies = dict(
                    getattr(self, "_refetch_new_cookies", {}) or {}
                )
                accumulated_cookies.update(refetched_cookies)
                self._refetch_new_cookies = accumulated_cookies

            # 风控已解除、token 直接可用：缓存结果并返回哨兵，让上层提前结束滑块流程、直接采用新 token
            if res.get("token_ok"):
                try:
                    self._refetch_token_ok = True
                    self._refetch_new_token = res.get("new_token")
                    refetch_device_id = str(res.get("device_id") or "").strip()
                    if refetch_device_id:
                        self.device_id = refetch_device_id
                except Exception:
                    pass
                return CAPTCHA_NOT_REQUIRED

            fresh_url = res.get("fresh_url")
            if fresh_url:
                return fresh_url

            # 未返回新链接：沿用原链接
            return None
        except Exception as e:
            logger.warning(f"【{self.cookie_id}】重新获取滑块验证链接失败，沿用原链接: {self._safe_str(e)}")
            return None

    async def _load_remote_captcha_config(self) -> dict | None:
        """读取全局"远程过滑块"配置（system_settings，仅管理员可配）。

        Returns:
            dict {url, secret, pass_cookies, device_id}；未配置或读取失败返回 None（此时走本机逻辑）。
            - pass_cookies 为 True 时表示"调用远程接口时传递账号 Cookie"（默认关闭）；
            - device_id 仅在 pass_cookies 开启时一并下发，供远程端在链接过期时重取新链接。
        """
        try:
            from common.db.session import async_session_maker
            from common.models.system_setting import SystemSetting
            from sqlalchemy import select

            async with async_session_maker() as session:
                rows = (await session.execute(
                    select(SystemSetting).where(
                        SystemSetting.key.in_(
                            [
                                "captcha.remote_service_url",
                                "captcha.remote_secret_key",
                                "captcha.remote_pass_cookies",
                            ]
                        )
                    )
                )).scalars().all()
            m = {r.key: (r.value or "") for r in rows}
            url = (m.get("captcha.remote_service_url") or "").strip()
            secret = (m.get("captcha.remote_secret_key") or "").strip()
            pass_cookies = (m.get("captcha.remote_pass_cookies") or "").strip().lower() == "true"
            if url and secret:
                return {
                    "url": url,
                    "secret": secret,
                    "pass_cookies": pass_cookies,
                    # 仅开启开关时下发 device_id，未开启则不携带任何账号信息
                    "device_id": (self.device_id or "") if pass_cookies else "",
                }
        except Exception as e:
            logger.warning(f"【{self.cookie_id}】读取远程过滑块配置失败（走本机逻辑）: {self._safe_str(e)}")
        return None

    async def handle_captcha_verification(self, res_json: dict) -> str | None:
        """处理滑块验证，返回新的cookies字符串"""
        try:
            import os

            # 检查消息接收冷却时间 - 收到消息后5分钟内不执行滑块验证
            current_time = time.time()
            time_since_last_message = current_time - self.last_message_received_time
            if self.last_message_received_time > 0 and time_since_last_message < self.message_cookie_refresh_cooldown:
                remaining_time = self.message_cookie_refresh_cooldown - time_since_last_message
                remaining_minutes = int(remaining_time // 60)
                remaining_seconds = int(remaining_time % 60)
                logger.info(f"【{self.cookie_id}】收到消息后冷却中，暂停滑块验证，还需等待 {remaining_minutes}分{remaining_seconds}秒")
                return None
            
            logger.info(f"【{self.cookie_id}】开始处理滑块验证...")

            # 获取验证URL
            verification_url = None
            data = res_json.get('data', {})
            if isinstance(data, dict) and 'url' in data:
                verification_url = data.get('url')

            if not verification_url:
                logger.info(f"【{self.cookie_id}】未找到验证URL，认为不需要滑块验证")
                return None

            logger.info(f"【{self.cookie_id}】验证URL: {verification_url}")
            
            # 同账号的“检查处理中状态 + 创建日志”使用同一临界区，避免并发重复滑块。
            log_id = None
            captcha_start_time = time.time()
            async with get_account_risk_control_lock(self.cookie_id):
                should_skip_captcha, existing_token = (
                    await self._get_processing_risk_control_skip_result("滑块处理")
                )
                if should_skip_captcha:
                    return None
                try:
                    from common.db.compat import db_manager
                    log_id = db_manager.add_risk_control_log(
                        cookie_id=self.cookie_id,
                        event_type='slider_captcha',
                        event_description=f'触发场景: Token刷新, URL: {verification_url}',
                        processing_status='processing'
                    )
                    if log_id:
                        logger.info(f"【{self.cookie_id}】风控日志记录成功，ID: {log_id}")
                except Exception as log_e:
                    logger.error(f"【{self.cookie_id}】记录风控日志失败: {log_e}")
            if not log_id:
                self.last_token_refresh_status = "failed_risk_log_create"
                logger.error(
                    f"【{self.cookie_id}】创建风控处理日志失败，本次不启动滑块任务"
                )
                return None

            async def _persist_refetched_cookie_updates() -> bool:
                """合并并写回重取验证链接时下发的 Cookie。"""
                refetched_cookies = dict(
                    getattr(self, "_refetch_new_cookies", {}) or {}
                )
                if not refetched_cookies:
                    return True
                try:
                    self.cookies.update(refetched_cookies)
                    self.cookies_str = "; ".join(
                        f"{key}={value}" for key, value in self.cookies.items()
                    )
                    if not await self.update_config_cookies():
                        logger.error(
                            f"【{self.cookie_id}】重取验证链接时下发的"
                            "Cookie未能写回数据库"
                        )
                        return False
                    logger.info(
                        f"【{self.cookie_id}】已合并并写回重取验证链接时下发的"
                        f" {len(refetched_cookies)} 个Cookie字段"
                    )
                    self._refetch_new_cookies = {}
                    return True
                except Exception as merge_error:
                    logger.error(
                        f"【{self.cookie_id}】合并重取验证链接Cookie失败: "
                        f"{self._safe_str(merge_error)}"
                    )
                    return False

            try:
                from app.services.captcha.slider_stealth import run_slider_verification_with_fallback

                # Clear the hand-off state only after this call owns the captcha
                # processing slot, so a concurrent call cannot erase its result.
                self._clear_refetch_state()

                # 读取全局"远程过滑块"配置（system_settings，仅管理员可配）。
                # 配置了则优先走远程接口；远程超时/不可用时回退本机逻辑。
                remote_config = await self._load_remote_captcha_config()

                # 真实鼠标任务先按权重排队，其他模式保持使用原浏览器任务专用线程池；
                # 两条路径都不占用 asyncio 默认线程池，避免饿死 aiohttp 的 DNS 解析。
                # run_slider_verification_with_fallback: 远程(可选)→真人/主引擎(Playwright)→DrissionPage 兜底
                # 返回 (是否成功, cookies, 通过引擎: remote/real_mouse/playwright/drissionpage/None)
                slider_args = (
                    f"{self.cookie_id}", verification_url, True, False, 20,
                    self.cookies_str, self._request_captcha_url_sync, remote_config,
                )
                selected_slider_mode = await refresh_slider_mode_from_database()
                if (
                    remote_config is None
                    and selected_slider_mode == SLIDER_MODE_REAL_MOUSE
                ):
                    # 本机真实鼠标任务先进入前置本地队列，再提交给原浏览器执行器。
                    success, cookies, captcha_engine = await real_mouse_weighted_runner.submit(
                        "local",
                        run_slider_verification_with_fallback,
                        *slider_args,
                        weight_class="local",
                        slider_mode=selected_slider_mode,
                    )
                else:
                    success, cookies, captcha_engine = await run_browser_task(
                        run_slider_verification_with_fallback,
                        *slider_args,
                        slider_mode=selected_slider_mode,
                    )

                # 重取验证链接的 Token 请求可能在任意结果分支下发新 Cookie（尤其是
                # _m_h5_tk）。浏览器流程结束后统一写回，不能只在 token_ok 分支处理。
                await _persist_refetched_cookie_updates()

                # 重取链接时发现 token 已可用（风控解除，无需滑块）：直接采用，跳过滑块结果处理。
                # The outer refresh_token() consumes _refetch_new_token first. A
                # cookie-only result is the only case that needs a token retry.
                if getattr(self, '_refetch_token_ok', False):
                    logger.info(f"【{self.cookie_id}】滑块流程中检测到 token 已可用，直接采用，跳过滑块验证")
                    captcha_duration = time.time() - captcha_start_time
                    if log_id:
                        try:
                            from common.db.compat import db_manager
                            db_manager.update_risk_control_log(
                                log_id=log_id,
                                processing_status='success',
                                processing_result=(
                                    f'重取验证链接时风控已解除，token 直接可用，无需滑块，'
                                    f'耗时: {captcha_duration:.2f}秒'
                                ),
                            )
                        except Exception as update_e:
                            logger.error(f"【{self.cookie_id}】更新风控日志失败: {update_e}")

                    return self.cookies_str

                if success and cookies:
                    logger.info(f"【{self.cookie_id}】滑块验证成功，获取到新的cookies")
                    # 打印滑块验证返回的全部cookies
                    logger.warning(f"【{self.cookie_id}】滑块验证返回的全部cookies: {cookies}")
                    
                    # 更新风控日志为成功状态
                    captcha_duration = time.time() - captcha_start_time
                    if log_id:
                        try:
                            from common.db.compat import db_manager
                            engine_label_map = {
                                'drissionpage': '兜底引擎(DrissionPage)',
                                'real_mouse': '真人鼠标引擎(RealMouse)',
                                'remote': '远程接口(Remote)',
                                'playwright': '主引擎(Playwright)',
                            }
                            engine_label = engine_label_map.get(captcha_engine, '主引擎(Playwright)')
                            db_manager.update_risk_control_log(
                                log_id=log_id,
                                processing_status='success',
                                captcha_engine=captcha_engine,
                                processing_result=f'滑块验证成功（{engine_label}），耗时: {captcha_duration:.2f}秒'
                            )
                        except Exception as update_e:
                            logger.error(f"【{self.cookie_id}】更新风控日志失败: {update_e}")

                    # 只提取x5sec相关的cookie值进行更新
                    updated_cookies = self.cookies.copy()
                    new_cookie_count = 0
                    updated_cookie_count = 0
                    x5sec_cookies = {}

                    for cookie_name, cookie_value in cookies.items():
                        cookie_name_lower = cookie_name.lower()
                        if cookie_name_lower.startswith('x5') or 'x5sec' in cookie_name_lower:
                            x5sec_cookies[cookie_name] = cookie_value

                    logger.info(f"【{self.cookie_id}】找到{len(x5sec_cookies)}个x5相关cookies")

                    # 滑块视觉验证通过但未下发任何 x5* cookie：服务端实际并未放行（典型场景为
                    # 浏览器/HTTP 出口 IP 不一致或风控环境异常）。若仍按"成功"返回 cookies_str，
                    # 上层 refresh_token 会认为滑块成功并立即重试 token 刷新，但 cookies 实际未变，
                    # token 接口必然再次返回 FAIL_SYS_USER_VALIDATE → 又触发滑块，形成死循环。
                    # 这里显式判定失败，让上层走 failed_captcha 分支（不计入禁用计数），
                    # 避免账号被误累加 _token_fetch_failures 直至 10 次自动禁用。
                    if not x5sec_cookies:
                        logger.error(
                            f"【{self.cookie_id}】滑块视觉验证通过但未获取到任何 x5 相关 cookie，"
                            f"判定为失败（浏览器返回的 cookies: {list(cookies.keys())}）"
                        )
                        captcha_duration = time.time() - captcha_start_time
                        if log_id:
                            try:
                                from common.db.compat import db_manager
                                db_manager.update_risk_control_log(
                                    log_id=log_id,
                                    processing_status='failed',
                                    processing_result=(
                                        f'滑块视觉通过但未下发 x5sec cookie（疑似环境/IP 风控），'
                                        f'耗时: {captcha_duration:.2f}秒'
                                    ),
                                )
                            except Exception as update_e:
                                logger.error(f"【{self.cookie_id}】更新风控日志失败: {update_e}")
                        return None

                    for cookie_name, cookie_value in x5sec_cookies.items():
                        if cookie_name in updated_cookies:
                            if updated_cookies[cookie_name] != cookie_value:
                                updated_cookies[cookie_name] = cookie_value
                                updated_cookie_count += 1
                        else:
                            updated_cookies[cookie_name] = cookie_value
                            new_cookie_count += 1

                    cookies_str = "; ".join([f"{k}={v}" for k, v in updated_cookies.items()])

                    # 更新数据库
                    try:
                        old_cookies_str = self.cookies_str
                        old_cookies_dict = self.cookies.copy()

                        self.cookies_str = cookies_str
                        self.cookies = updated_cookies
                        
                        # 打印更新后的x5sec值
                        logger.warning(f"【{self.cookie_id}】准备保存到数据库的x5sec: {updated_cookies.get('x5sec', '无')}")

                        if not await self.update_config_cookies():
                            raise RuntimeError("滑块Cookie写回数据库失败")
                        logger.info(f"【{self.cookie_id}】滑块验证成功后，数据库cookies已自动更新")
                        logger.info(f"【{self.cookie_id}】滑块验证成功: 新增{new_cookie_count}个x5, 更新{updated_cookie_count}个x5")

                        await self.send_token_refresh_notification(
                            f"滑块验证成功，cookies已自动更新到数据库",
                            "captcha_success_auto_update"
                        )

                    except Exception as update_e:
                        logger.error(f"【{self.cookie_id}】自动更新数据库cookies失败: {self._safe_str(update_e)}")
                        self.cookies_str = old_cookies_str
                        self.cookies = old_cookies_dict
                        return None

                    return cookies_str
                else:
                    remote_fail_reason = None
                    captcha_engine_for_log = captcha_engine
                    if isinstance(captcha_engine, str) and captcha_engine.startswith("remote:"):
                        remote_fail_reason = captcha_engine.split(":", 1)[1].strip() or "远程过滑块未通过"
                        captcha_engine_for_log = "remote"

                    if remote_fail_reason:
                        logger.error(f"【{self.cookie_id}】滑块验证失败: {remote_fail_reason}")
                    else:
                        logger.error(f"【{self.cookie_id}】滑块验证失败")
                    
                    # 更新风控日志为失败状态
                    captcha_duration = time.time() - captcha_start_time
                    if log_id:
                        try:
                            from common.db.compat import db_manager
                            processing_result = (
                                f'远程过滑块失败：{remote_fail_reason}，耗时: {captcha_duration:.2f}秒'
                                if remote_fail_reason
                                else f'滑块验证失败，耗时: {captcha_duration:.2f}秒'
                            )
                            update_kwargs = {
                                "processing_status": "failed",
                                "processing_result": processing_result,
                            }
                            if captcha_engine_for_log == "remote":
                                update_kwargs["captcha_engine"] = "remote"
                            if remote_fail_reason:
                                update_kwargs["error_message"] = remote_fail_reason
                            db_manager.update_risk_control_log(
                                log_id=log_id,
                                **update_kwargs,
                            )
                        except Exception as update_e:
                            logger.error(f"【{self.cookie_id}】更新风控日志失败: {update_e}")
                    
                    return None

            except ImportError as import_e:
                logger.error(f"【{self.cookie_id}】滑块验证导入失败: {import_e}")
                
                # 更新风控日志为异常状态
                if log_id:
                    try:
                        from common.db.compat import db_manager
                        db_manager.update_risk_control_log(
                            log_id=log_id,
                            processing_status='error',
                            error_message='滑块验证模块未安装'
                        )
                    except Exception:
                        pass
                
                await self.send_token_refresh_notification(
                    f"滑块验证功能不可用，请安装Playwright",
                    "captcha_dependency_missing"
                )
                return None

            except asyncio.CancelledError:
                # 任务被取消，记录日志并重新抛出
                logger.warning(f"【{self.cookie_id}】滑块验证任务被取消")
                await _persist_refetched_cookie_updates()
                captcha_duration = time.time() - captcha_start_time
                if log_id:
                    try:
                        from common.db.compat import db_manager
                        db_manager.update_risk_control_log(
                            log_id=log_id,
                            processing_status='cancelled',
                            processing_result=f'任务被取消，耗时: {captcha_duration:.2f}秒'
                        )
                    except Exception:
                        pass
                raise

            except Exception as stealth_e:
                logger.error(f"【{self.cookie_id}】滑块验证异常: {self._safe_str(stealth_e)}")
                await _persist_refetched_cookie_updates()
                
                # 更新风控日志为异常状态
                captcha_duration = time.time() - captcha_start_time
                if log_id:
                    try:
                        from common.db.compat import db_manager
                        db_manager.update_risk_control_log(
                            log_id=log_id,
                            processing_status='error',
                            processing_result=f'滑块验证异常，耗时: {captcha_duration:.2f}秒',
                            error_message=self._safe_str(stealth_e)
                        )
                    except Exception:
                        pass
                
                return None

        except asyncio.CancelledError:
            logger.warning(f"【{self.cookie_id}】处理滑块验证时任务被取消")
            raise
        except Exception as e:
            logger.error(f"【{self.cookie_id}】处理滑块验证时出错: {self._safe_str(e)}")
            return None

    # ==================== Cookie更新并重启 ====================

    async def update_cookies_and_restart(self, new_cookies_str: str):
        """更新cookies并重启任务"""
        try:
            logger.info(f"【{self.cookie_id}】开始更新cookies并重启任务...")

            if not new_cookies_str or not new_cookies_str.strip():
                logger.error(f"【{self.cookie_id}】新cookies为空，无法更新")
                return False

            try:
                new_cookies_dict = trans_cookies(new_cookies_str)
                if not new_cookies_dict:
                    logger.error(f"【{self.cookie_id}】新cookies解析失败")
                    return False
            except Exception as parse_e:
                logger.error(f"【{self.cookie_id}】新cookies解析异常: {self._safe_str(parse_e)}")
                return False

            # 合并cookies
            current_cookies_dict = trans_cookies(self.cookies_str)
            merged_cookies_dict = current_cookies_dict.copy()

            for key, value in new_cookies_dict.items():
                merged_cookies_dict[key] = value

            merged_cookies_str = '; '.join([f"{k}={v}" for k, v in merged_cookies_dict.items()])

            # 更新实例cookies
            self.cookies_str = merged_cookies_str
            self.cookies = merged_cookies_dict

            # 更新数据库
            if not await self.update_config_cookies():
                self.cookies_str = "; ".join(
                    f"{key}={value}" for key, value in current_cookies_dict.items()
                )
                self.cookies = current_cookies_dict
                logger.error(f"【{self.cookie_id}】Cookie写回数据库失败，本次不重启")
                return False
            logger.info(f"【{self.cookie_id}】cookies已更新到数据库")

            # Cookie已变更，清除旧的Token缓存（新Cookie需要重新获取Token）
            await self._delete_cached_token()

            # 重启实例
            await self.restart_instance("Cookie更新后重启")
            return True

        except Exception as e:
            logger.error(f"【{self.cookie_id}】更新cookies并重启失败: {self._safe_str(e)}")
            return False


    # ==================== Token刷新核心逻辑 ====================

    async def refresh_token(self, captcha_retry_count: int = 0, token_expiry_retry_count: int = 0):
        """续签、首次检查及必要密码登录走同一持久预算，停止旧递归恢复。"""
        from common.services.account_recovery import AccountRecovery
        from common.services.account_credentials import CredentialRejected, credential_failure
        runtime = getattr(self.parent, '_account_runtime', None)
        expected = None
        if runtime:
            await runtime.lease.check()
            expected = (*runtime.version, runtime.generation)
        recovery = AccountRecovery(self.cookie_id, self.parent.user_id)
        # 成功缓存不属于恢复尝试；过期缓存不再冒充可用 Token。
        cached = await self._get_cached_token(allow_expired=False)
        if cached:
            return await self._use_cached_token(cached)
        job = await recovery.start('renewal', expected=expected)
        if not job:
            self.last_token_refresh_status = 'skipped_cooldown'
            return None
        token_result = None
        async def renew():
            nonlocal token_result
            async with recovery.sessions() as db:
                a = await recovery.account(db)
                recovery.validate(a,job['id'])
                cookie, identity = a.cookie, a.unb
                proxy = {k:getattr(a,k) for k in ('proxy_type','proxy_host','proxy_port','proxy_user','proxy_pass')}
            if runtime: await runtime.lease.check()
            result = await request_im_token_with_fallback(cookie, self.device_id,
                proxy_config=proxy, timeout_seconds=30)
            token_result = extract_im_access_token(result.response_json)
            if not token_result: raise CredentialRejected(credential_failure(result))
            parts = trans_cookies(cookie); parts.update(result.response_cookies)
            if parts.get('unb') != str(identity): raise CredentialRejected('identity_mismatch')
            return '; '.join(f'{k}={v}' for k,v in parts.items())
        accepted = await recovery.run(job['id'], renew=renew, login=recovery.password_once)
        if not accepted:
            self.current_token = None
            self.last_token_refresh_status = 'skipped_cooldown'
            return None
        a = await self._load_account_record()
        self.cookies_str = a.cookie; self.cookies = trans_cookies(a.cookie)
        if runtime:
            from common.services import account_policy
            state = account_policy.snapshot(a)
            if state['generation'] != runtime.generation:
                return None
            runtime.version = (state['credential_version'],state['config_version'])
            await runtime.lease.check()
        if not token_result:
            self.last_token_refresh_status = 'credentials_verified'
            await self.restart_instance(reason='密码恢复已验证凭据')
            return None
        self.current_token = token_result
        self.last_token_refresh_time = time.time()
        self.last_token_refresh_status = 'success'
        await self._set_cached_token(token_result,self.device_id)
        return token_result

    async def try_password_login_refresh(self, trigger_reason: str = "Session过期"):
        from common.services.account_recovery import AccountRecovery
        from common.services import account_policy as policy
        from common.services.account_execution import ExecutionLost
        reason = 'invalid_credentials' if ('Session' in trigger_reason or '认证失败' in trigger_reason) else 'unknown'
        runtime = getattr(self.parent, '_account_runtime', None)
        expected = None
        if runtime:
            await runtime.lease.check()
            expected = (*runtime.version, runtime.generation)
        recovery = AccountRecovery(self.cookie_id, self.parent.user_id)
        job = await recovery.start(reason, expected=expected)
        if not job:
            return 'skipped_cooldown'
        accepted = await recovery.run(job['id'], renew=recovery.renew_once, login=recovery.password_once)
        if accepted:
            account = await self._load_account_record()
            self.cookies_str = account.cookie
            self.cookies = trans_cookies(account.cookie)
            # 更新版本后旧执行方退场，接任重新做握手；这里不发出业务或另取租约。
            if runtime:
                await runtime.close()
            await self.restart_instance(reason='持久恢复任务凭据已验证')
            return True
        self.last_token_refresh_status = 'skipped_cooldown'
        return 'skipped_cooldown'

    async def verify_cookie_validity(self) -> dict:
        """验证Cookie的有效性，通过实际调用API测试"""
        logger.info(f"【{self.cookie_id}】开始验证Cookie有效性（使用真实API调用）...")
        
        result = {
            'valid': True,
            'confirm_api': None,
            'image_api': None,
            'details': []
        }
        
        try:
            logger.info(f"【{self.cookie_id}】测试图片上传API...")
            
            import tempfile
            import os
            from PIL import Image
            
            temp_dir = tempfile.gettempdir()
            test_image_path = os.path.join(temp_dir, f'cookie_test_{self.cookie_id}.png')
            
            try:
                img = Image.new('RGB', (1, 1), color='white')
                img.save(test_image_path, 'PNG')
                logger.info(f"【{self.cookie_id}】已创建测试图片: {test_image_path}")
                
                from common.utils.image_uploader import ImageUploader
                uploader = ImageUploader(cookies_str=self.cookies_str, account_id=self.cookie_id, owner_id=self.parent.user_id)
                
                await uploader.create_session()
                
                try:
                    upload_result = await uploader.upload_image(test_image_path)
                finally:
                    await uploader.close_session()
                
                if upload_result:
                    logger.info(f"【{self.cookie_id}】✅ 图片上传API验证通过")
                    result['image_api'] = True
                    result['details'].append("图片上传API: 通过验证")
                else:
                    logger.warning(f"【{self.cookie_id}】❌ 图片上传API验证失败")
                    result['image_api'] = False
                    result['valid'] = False
                    result['details'].append("图片上传API: 上传失败，可能Cookie已失效")
                
            finally:
                if os.path.exists(test_image_path):
                    try:
                        os.remove(test_image_path)
                        logger.info(f"【{self.cookie_id}】已清理测试图片")
                    except Exception:
                        pass
                        
        except Exception as e:
            logger.error(f"【{self.cookie_id}】图片上传API验证异常: {self._safe_str(e)}")
            result['image_api'] = True
            result['details'].append(f"图片上传API: 调用异常(可能非Cookie问题)")
        
        result['details'] = '; '.join(result['details'])
        logger.info(f"【{self.cookie_id}】Cookie有效性验证完成: {result}")
        return result

