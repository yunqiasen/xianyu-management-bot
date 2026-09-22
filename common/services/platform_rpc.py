"""Fixed product/support MTOP adapters; no caller-controlled host, Cookie or proxy."""
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Existing product and support operations only. Order delivery/purchase have their own ledger.
PRODUCT_APIS = frozenset({
    'mtop.idle.pc.backend.idleitem.preget', 'mtop.idle.pc.idleitem.preget',
    'mtop.idle.pc.backend.idleitem.publish', 'mtop.idle.pc.idleitem.publish',
    'mtop.idle.pc.backend.idleitem.edit', 'mtop.idle.pc.backend.idleitem.editdetail',
    'mtop.alibaba.idle.seller.pc.common.item.search',
    'mtop.alibaba.idle.seller.pc.item.batch.offline', 'mtop.alibaba.idle.seller.pc.item.delete',
    'mtop.alibaba.idle.seller.pc.item.info.update', 'mtop.idle.web.xyh.item.list',
    'mtop.taobao.idle.kgraph.pc.property.recommend', 'mtop.taobao.idle.logistic.address.list.query',
    'mtop.taobao.idle.detail', 'mtop.taobao.idle.item.detail', 'mtop.taobao.idle.item.pc.detail',
    'mtop.taobao.idle.pc.detail', 'mtop.taobao.idlemtopdetail',
    'mtop.taobao.idle.merchant.rate.list',
    'mtop.taobao.idlemtopsearch.pc.search',
    'mtop.taobao.idlemessage.pc.blacklist.add', 'mtop.taobao.idlemessage.pc.blacklist.query',
    'mtop.taobao.idlemessage.pc.blacklist.remove', 'mtop.taobao.idlemessage.pc.user.query',
    'mtop.alibaba.idle.seller.pc.datacompass.singleuser.browse.summary',
    'mtop.alibaba.idle.seller.pc.datacompass.singleuser.seller.summary',
})


class PlatformRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    api: str
    version: str = Field(pattern=r'^\d+(\.\d+){0,2}$', max_length=16)
    data: dict
    extra_params: dict[str, str] | None = None
    extra_headers: dict[str, str] | None = Field(default=None, max_length=2)
    app_key: Literal['34839810'] = '34839810'
    origin: Literal['https://www.goofish.com', 'https://seller.goofish.com'] = 'https://www.goofish.com'
    referer: str = Field(default='https://www.goofish.com/', max_length=2048)
    form_field: Literal['data'] = 'data'
    request_method: Literal['GET', 'POST'] = 'POST'

    @field_validator('extra_headers')
    @classmethod
    def business_headers_only(cls, value):
        if value is None:
            return value
        if set(value) - {'idle_site_biz_code', 'idle_user_group_member_id'}:
            raise ValueError('platform_header_not_allowed')
        if value.get('idle_site_biz_code', 'COMMONPRO') != 'COMMONPRO':
            raise ValueError('platform_site_not_allowed')
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{0,80}', value.get('idle_user_group_member_id', '')):
            raise ValueError('platform_group_not_allowed')
        return value

    @field_validator('api')
    @classmethod
    def known_api(cls, value):
        if value not in PRODUCT_APIS:
            raise ValueError('platform_operation_not_allowed')
        return value

    @field_validator('referer')
    @classmethod
    def fixed_referer(cls, value):
        from urllib.parse import urlsplit
        url = urlsplit(value)
        if (url.scheme != 'https' or url.hostname not in {'www.goofish.com', 'seller.goofish.com'}
                or url.port not in (None,443) or url.username or url.password):
            raise ValueError('platform_referer_not_allowed')
        return value


class PlatformGateway:
    def __init__(self, client, sessions):
        self.client, self.sessions = client, sessions

    async def call(self, account_id, owner_id, api, version, data, *, request_id=None, **options):
        from common.services.account_dispatch import DispatchError
        payload = PlatformRequest(api=api, version=version, data=data, **options)
        try:
            async with self.sessions() as session:
                operation = await self.client.submit(owner_id=owner_id, account_id=account_id,
                    request_id=request_id or 'platform-' + uuid4().hex, command='platform_request',
                    payload=payload.model_dump(), session=session)
            if operation.status == 'confirmed':
                return {'success':True, 'account_invalid':False, 'res':operation.result, 'error':''}
            return {'success':False, 'account_invalid':operation.error_code in {'invalid_credentials','verification_required'},
                    'res':operation.result, 'error':operation.error_code or 'platform_result_unverified',
                    'retry_after':operation.retry_after,
                    '_request_status_unknown':operation.status in {'unknown','submitted'}}
        except DispatchError as exc:
            return {'success':False, 'res':None, 'error':exc.code, 'account_invalid':False}


async def execute_platform_request(context, payload):
    from common.services.xianyu_mtop import mtop_call
    from common.services.account_dispatch import DispatchError, PlatformFailure
    request = PlatformRequest.model_validate(payload)
    result = await mtop_call(context.request.account_id, '', owner_id=context.request.owner_id,
                             **request.model_dump())
    if not result.get('success'):
        error = result.get('error') or 'platform_result_unverified'
        if result.get('_request_status_unknown'):
            raise DispatchError(error)
        raw = result.get('res')
        # Capability detection needs the explicit business code, not sensitive challenge data.
        receipt = {'ret':raw['ret']} if isinstance(raw, dict) and isinstance(raw.get('ret'), list) else None
        raise PlatformFailure(error, retry_after=result.get('retry_after'), result=receipt)
    return result['res']
