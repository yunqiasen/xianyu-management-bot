"""凭据首次检查；只有 Token 接口确认有效才允许回写，不建立交易。"""
from common.services import account_policy
from common.services.im_token_api import request_im_token, extract_im_access_token
from common.utils.xianyu_utils import generate_device_id, trans_cookies


async def validate_credentials(cookie, expected_unb, proxy_config):
    parts = trans_cookies(cookie)
    identity = parts.get('unb')
    if not identity or (expected_unb and identity != str(expected_unb)):
        raise ValueError('凭据账号身份不一致')
    account_policy.proxy_url(proxy_config)
    result = await request_im_token(cookie, generate_device_id(identity), proxy_config=proxy_config)
    if not extract_im_access_token(result.response_json):
        raise CredentialRejected(credential_failure(result))
    parts.update(result.response_cookies)
    if parts.get('unb') != identity:
        raise ValueError('验证返回的账号身份不一致')
    return '; '.join(f'{k}={v}' for k, v in parts.items())


class CredentialRejected(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def credential_failure(result):
    """只把明确失效交给密码恢复；网络/限流/验证各自停止。"""
    from common.services.im_token_api import is_session_expired_token_result
    if result.status_code == 429: return 'rate_limit'
    if result.status_code >= 500: return 'network'
    text = str(result.response_json).lower()
    if any(word in text for word in ('captcha', 'rgv587', '验证', 'punish')): return 'verification'
    if any(word in text for word in ('limit', '频繁', 'traffic')): return 'rate_limit'
    if is_session_expired_token_result(result): return 'invalid_credentials'
    return 'unknown'
