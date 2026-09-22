"""Public product outcomes: never interpret a missing response as an empty list."""
def product_error_status(message: str) -> str:
    text = str(message).upper()
    if any(x in text for x in ('TRAFFIC', 'LIMIT', '限流', '请求过快', 'RGV587')):
        return 'rate_limited'
    if any(x in text for x in ('INVALID_CREDENTIALS', 'SESSION_EXPIRED', 'TOKEN_EXPIRED', 'TOKEN_EXOIRED', '登录失效', '重新登录')):
        return 'credentials_expired'
    if any(x in text for x in ('VERIFICATION_REQUIRED', 'VALIDATE', '验证', 'CAPTCHA')):
        return 'verification_required'
    if 'PROXY' in text or '代理' in text:
        return 'proxy_error'
    if any(x in text for x in ('SCHEMA', '结构', 'JSON')):
        return 'schema_error'
    return 'transport_error'


def product_retry_hint(status, response=None):
    """Page-local hint only; the account dispatcher owns the actual retry budget."""
    response = response if isinstance(response, dict) else {}
    actions = {'credentials_expired':'restore_session', 'verification_required':'manual_verification',
               'proxy_error':'repair_proxy', 'schema_error':'inspect_response', 'storage_error':'repair_storage'}
    delay = None
    if status in ('rate_limited', 'transport_error'):
        import random
        value = response.get('retry_after')
        delay = min(max(int(value), 1), 86400) if isinstance(value, (int, float)) and value > 0 else random.randint(48,72)
    return {'retry_after': delay, 'retry_action': actions.get(status, 'wait_budget')}


def validate_product_page(result):
    if not isinstance(result, dict) or not isinstance(result.get('items'), list):
        return False
    if 'has_more' in result and not isinstance(result['has_more'], bool):
        return False
    return all(isinstance(i, dict) and str(i.get('id') or i.get('item_id') or '').strip() for i in result['items'])


class ProductCapabilityError(RuntimeError):
    """An unknown account type must never select a different platform product API."""
    def __init__(self, result):
        self.code = result.get('error') or 'capability_unverified'
        self.status = product_error_status(self.code)
        self.retry_after = result.get('retry_after')
        super().__init__(self.code)

    def response(self):
        return {'success':False, 'status':self.status, 'message':self.code,
                **product_retry_hint(self.status, {'retry_after':self.retry_after})}
