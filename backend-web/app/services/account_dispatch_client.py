"""后台薄适配；scheduler 直接 import common.services.account_dispatch.AccountDispatchClient。"""
from common.services.account_dispatch import AccountDispatchClient as SharedAccountDispatchClient


class AccountDispatchClient(SharedAccountDispatchClient):
    def __init__(self, base_url=None, token=None, **kwargs):
        if base_url is None or token is None:
            from app.core.config import get_settings
            settings = get_settings()
            if base_url is None: base_url = settings.websocket_service_url
            if token is None: token = settings.internal_api_token
        super().__init__(base_url, token, **kwargs)
