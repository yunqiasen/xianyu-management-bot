"""Version-fenced MTOP transport using the existing executor HTTP session.

One external attempt per dispatch. Credential recovery belongs to the persistent
account recovery runner; ambiguous responses never cause an inline replay.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Dict, Optional

import aiohttp
from loguru import logger

from common.utils.cookie_refresh import (
    is_session_expired_error,
)
from common.utils.xianyu_utils import generate_sign, trans_cookies

# 令牌过期/缺失标志（命中则用 Set-Cookie 刷新 _m_h5_tk 后重试）
# 注意：闲鱼历史拼写为 EXOIRED，同时兼容标准拼写 EXPIRED 与 EMPTY（首次无令牌）
_TOKEN_EXPIRED_MARKERS = (
    "FAIL_SYS_TOKEN_EXOIRED",
    "FAIL_SYS_TOKEN_EXPIRED",
    "FAIL_SYS_TOKEN_EMPTY",
    "令牌过期",
    "令牌为空",
)

# 触发验证/被挤爆/机器检测等风控标志（命中则应切换账号重试）
# 对外公开：发布账号能力检测需要判断「失败是否属于风控」，直接复用本元组避免两处维护出现漂移
VALIDATE_MARKERS = (
    "FAIL_SYS_USER_VALIDATE",
    "RGV587",
    "FAIL_SYS_ILLEGAL_ACCESS",
    "FAIL_BIZ_WUA_IS_MACHINE",  # WUA机器检测（下单"无法购买哦"），换账号重试
    "WUA_IS_MACHINE",
    "哎哟喂",
    "挤爆",
    "punish",
    "captcha",
    "validate",
)

# 单次调用内最大尝试次数（令牌刷新/网络异常重试）
MTOP_BASE = 'https://h5api.m.goofish.com'


def extract_punish_url(res_json: Optional[Dict[str, Any]]) -> str:
    """从 mtop 返回中提取风控验证（punish）链接。

    触发验证类风控时，闲鱼在 data.url 下发 punish?x5secdata=... 验证链接，
    调用方可凭该链接走远程过风控服务求解，拿到 x5sec 后重试。

    Args:
        res_json: mtop 接口原始返回 JSON
    Returns:
        punish 验证链接；无则返回空字符串
    """
    if not isinstance(res_json, dict):
        return ""
    data_node = res_json.get("data")
    if not isinstance(data_node, dict):
        return ""
    return str(data_node.get("url") or "").strip()


async def fetch_proxy_from_api(api_url: str, account_id: str = "") -> Optional[str]:
    """调用代理 API 获取一个 HTTP 代理，返回 'http://host:port'，失败返回 None（直连）。

    参照系统设置中的代理使用方式：GET api_url，响应为纯文本 IP:PORT（多行时取第一非空行），
    严格解析 host:port 后拼成 http 代理 URL。失败（非200/空/格式异常/超时）统一返回 None。
    """
    if not api_url:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(api_url) as resp:
                if resp.status != 200:
                    logger.warning(f"【{account_id}】代理API返回状态码 {resp.status}，本次直连")
                    return None
                text = (await resp.text() or "").strip()
        if not text:
            logger.warning(f"【{account_id}】代理API返回内容为空，本次直连")
            return None
        # 取第一非空行（兼容多行返回的代理供应商）
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first_line:
            return None
        # 严格解析 host:port，避免误返回 HTML/JSON 被错误使用
        matched = re.match(r"^([^\s:]+):(\d{1,5})$", first_line)
        if not matched:
            logger.warning(f"【{account_id}】代理API返回格式无法解析: {first_line!r}，本次直连")
            return None
        host = matched.group(1)
        port = int(matched.group(2))
        if not (1 <= port <= 65535):
            logger.warning(f"【{account_id}】代理端口非法: {port}，本次直连")
            return None
        logger.info(f"【{account_id}】代理API获取成功: http://{host}:{port}")
        return f"http://{host}:{port}"
    except asyncio.TimeoutError:
        logger.warning(f"【{account_id}】代理API调用超时（10s），本次直连")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"【{account_id}】代理API调用异常: {exc}，本次直连")
        return None


async def mtop_call(
    account_id: str,
    cookies_str: str,
    api: str,
    version: str,
    data: dict,
    *,
    owner_id: Optional[int] = None,
    extra_params: Optional[Dict[str, str]] = None,
    proxy: Optional[str] = None,
    app_key: str = "34839810",
    origin: str = "https://www.goofish.com",
    referer: str = "https://www.goofish.com/",
    extra_headers: Optional[Dict[str, str]] = None,
    form_field: str = "data",
    request_method: str = "POST",
) -> Dict[str, Any]:
    """调用闲鱼 mtop 接口，统一处理令牌过期/Session过期/风控。

    Args:
        proxy: 代理地址URL（http://host:port 或 socks5://user:pass@host:port），空则直连。
        request_method: mtop 请求方法，默认 POST；视频初始化等抓包为 GET 的接口可指定 GET。

    Returns:
        {
          success: bool,
          account_invalid: bool,   # True 表示需切换账号（Session过期/验证/挤爆）
          res: dict|None,          # 接口原始返回 JSON
          error: str,
          cookies_str: str,        # 可能因令牌刷新而更新，调用方应回写实例并用于后续请求
          punish_url: str,         # 仅风控验证类失败时有值：punish 验证链接（可走远程过风控）
        }
    """
    from common.services.account_dispatch import CURRENT_OPERATION, DispatchError
    context = CURRENT_OPERATION.get()
    def failure(code, *, unknown=False, res=None, invalid=False, retry_after=None):
        return {"success": False, "account_invalid": invalid, "res": res,
                "error": code, "cookies_str": cookies_str,
                "_request_status_unknown": unknown, "retry_after": retry_after}
    if context is None and owner_id is not None:
        if proxy is not None:
            return failure('caller_proxy_override')
        try:
            from app.core.config import get_settings
            from common.db.session import async_session_maker
            from common.services.account_dispatch import AccountDispatchClient
            from common.services.platform_rpc import PlatformGateway
            settings = get_settings()
            gateway = PlatformGateway(AccountDispatchClient(settings.websocket_service_url,
                settings.internal_api_token), async_session_maker)
            result = await gateway.call(account_id, owner_id, api, version, data,
                extra_params=extra_params, extra_headers=extra_headers, app_key=app_key, origin=origin, referer=referer,
                form_field=form_field, request_method=request_method.upper())
            # Return only the caller's unchanged value for old adapters; workers never export cookies.
            return {**result, 'cookies_str': cookies_str}
        except (ValueError, RuntimeError, AttributeError):
            return failure('platform_gateway_unavailable')
    if (context is None or context.request.account_id != account_id
            or owner_id is None or context.request.owner_id != owner_id):
        return failure('missing_execution_context')
    if request_method.upper() not in {'GET', 'POST'}:
        return failure('unsupported_request_method')
    session = getattr(context.live, 'session', None)
    if session is None:
        return failure('executor_http_unavailable')
    # The executor session owns its proxy connector. A caller cannot change it.
    bound_proxy = context.live._get_proxy_url()
    if proxy is not None and proxy != bound_proxy:
        return failure('proxy_binding_mismatch')
    try:
        account = await context.check()
        current_cookies = account.cookie
        cookies = trans_cookies(current_cookies)
        token = cookies.get('_m_h5_tk', '').split('_')[0]
        timestamp = str(int(time.time() * 1000))
        data_value = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        params = dict(extra_params or {})
        params.update(jsv='2.7.2', appKey=app_key, t=timestamp,
                      sign=generate_sign(timestamp, token, data_value, app_key=app_key),
                      v=version, type='originaljson', accountSite='xianyu',
                      dataType='json', timeout='20000', api=api,
                      sessionOption='AutoLoginOnly', spm_cnt='a21ybx.item.0.0')
        headers = dict(extra_headers or {})
        headers.update({'Accept':'application/json', 'Content-Type':'application/x-www-form-urlencoded',
                        'Origin':origin, 'Referer':referer, 'Cookie':current_cookies})
        await context.before_external()
        url = f'{MTOP_BASE}/h5/{api}/{version}/'
        if request_method.upper() == 'GET':
            request = session.get(url, params={**params, form_field:data_value},
                                  headers=headers, allow_redirects=False)
        else:
            request = session.post(url, params=params, data={form_field:data_value},
                                   headers=headers, allow_redirects=False)
        async with request as response:
            if response.status == 429:
                from common.services.account_request_budget import parse_retry_after
                hint = parse_retry_after(response.headers.get('Retry-After'))
                if hint is not None: await context.budget.defer(account_id, hint)
                return failure('platform_rate_limited', retry_after=hint)
            if response.status != 200:
                return failure('platform_http_unverified', unknown=True)
            result = await response.json(content_type=None)
        await context.check()
        ret = result.get('ret') if isinstance(result, dict) else None
        if not isinstance(ret, list) or not ret or not isinstance(ret[0], str):
            return failure('missing_positive_ack', unknown=True)
        if ret[0].startswith('SUCCESS::'):
            return {'success':True, 'account_invalid':False, 'res':result,
                    'error':'', 'cookies_str':current_cookies}
        if any(marker in ret[0] for marker in _TOKEN_EXPIRED_MARKERS) or is_session_expired_error(ret):
            return failure('invalid_credentials', res=result, invalid=True)
        if any(marker in ret[0] for marker in VALIDATE_MARKERS):
            return {**failure('verification_required', res=result, invalid=True),
                    'punish_url':extract_punish_url(result)}
        return failure('platform_rejected', res=result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        from common.services.account_request_budget import BudgetError
        code = exc.code if isinstance(exc, (DispatchError, BudgetError)) else 'request_unverified'
        return failure(code, unknown=context.external_started, retry_after=getattr(exc, 'retry_after', None))


__all__ = ['mtop_call', 'fetch_proxy_from_api', 'extract_punish_url', 'VALIDATE_MARKERS']
