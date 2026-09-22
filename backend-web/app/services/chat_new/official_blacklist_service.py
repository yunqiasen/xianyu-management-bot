"""闲鱼官方会话黑名单操作（拉黑/取消拉黑/查询）"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
from common.services.account_dispatch import before_platform_request
from common.services.account_policy import proxy_url

from common.utils.xianyu_utils import generate_sign, trans_cookies


API_VERSIONS = {
    "query": ("mtop.taobao.idlemessage.pc.blacklist.query", "1.0"),
    "add": ("mtop.taobao.idlemessage.pc.blacklist.add", "2.0"),
    "remove": ("mtop.taobao.idlemessage.pc.blacklist.remove", "1.0"),
}


async def official_blacklist_request(cookies_str: str, session_id: str, action: str, *, account) -> dict[str, Any]:
    api, version = API_VERSIONS[action]
    data_val = json.dumps({"sessionId": str(session_id)}, separators=(",", ":"))
    timestamp = str(int(time.time() * 1000))
    cookies = trans_cookies(cookies_str)
    token = cookies.get("_m_h5_tk", "").split("_")[0]
    params = {
        "jsv": "2.7.2",
        "appKey": "34839810",
        "t": timestamp,
        "sign": generate_sign(timestamp, token, data_val),
        "v": version,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": api,
        "sessionOption": "AutoLoginOnly",
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": cookies_str.replace("\n", "").replace("\r", ""),
        "origin": "https://www.goofish.com",
        "referer": "https://www.goofish.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36",
    }
    url = f"https://h5api.m.goofish.com/h5/{api}/{version}/"
    # 只能由持有账号执行权的上下文调用；代理配置/代理故障均不降级直连。
    proxy = proxy_url({key: getattr(account, key, None) for key in
        ('proxy_type', 'proxy_host', 'proxy_port', 'proxy_user', 'proxy_pass')})
    await before_platform_request(account.account_id)
    async with httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=20, follow_redirects=False) as session:
        response = await session.post(url, params=params, data={"data": data_val}, headers=headers)
        response.raise_for_status()
        result = response.json()
    ret = result.get("ret", [])
    if not any("SUCCESS" in str(item) for item in ret):
        raise RuntimeError(str(ret[0] if ret else "闲鱼黑名单接口调用失败"))
    return result.get("data", {}) or {}


def parse_blacklist_status(data):
    value = data.get('isInBlack')
    if value is True or value == 1 or value in ('true', '1'):
        return True
    if value is False or value == 0 or value in ('false', '0'):
        return False
    raise ValueError('平台名单响应缺少明确状态')
