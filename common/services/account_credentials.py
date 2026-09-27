"""凭据首次检查；只有 Token 接口确认有效才允许回写，不建立交易。"""
from common.services import account_policy
from common.services.im_token_api import request_im_token, extract_im_access_token
from common.utils.xianyu_utils import generate_device_id, trans_cookies


async def validate_credentials(cookie, expected_unb, proxy_config, *, initialize_token=False):
    parts = trans_cookies(cookie)
    identity = parts.get('unb')
    if not identity or (expected_unb and identity != str(expected_unb)):
        raise ValueError('凭据账号身份不一致')
    account_policy.proxy_url(proxy_config)
    device_id = generate_device_id(identity)
    # 扫码的新 Cookie 尚无 MTOP 签名令牌；只补一次平台明确要求的握手。
    # 默认仍为单次请求，自动恢复继续由外层共享预算控制。
    for attempt in range(2 if initialize_token else 1):
        result = await request_im_token(cookie, device_id, proxy_config=proxy_config)
        merged = {**parts, **result.response_cookies}
        if merged.get('unb') != identity:
            raise ValueError('验证返回的账号身份不一致')
        if extract_im_access_token(result.response_json):
            return '; '.join(f'{k}={v}' for k, v in merged.items())
        reason = credential_failure(result)
        new_token = result.response_cookies.get('_m_h5_tk')
        if not (initialize_token and attempt == 0 and result.status_code == 200
                and reason == 'token_initialization' and new_token
                and new_token != parts.get('_m_h5_tk')):
            raise CredentialRejected(reason)
        parts = merged
        cookie = '; '.join(f'{k}={v}' for k, v in parts.items())


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
    body = result.response_json
    ret = body.get('ret', []) if isinstance(body, dict) else []
    ret = [ret] if isinstance(ret, str) else (ret if isinstance(ret, list) else [])
    codes = {str(value).split('::', 1)[0] for value in ret}
    if codes and codes <= {'FAIL_SYS_TOKEN_EMPTY', 'FAIL_SYS_TOKEN_EXOIRED', 'FAIL_SYS_TOKEN_EXPIRED'}:
        return 'token_initialization'
    return 'unknown'
