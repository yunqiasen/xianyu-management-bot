"""Fixed monitor command for the account executor; no account/browser ownership here."""
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Literal
import aiohttp
from pydantic import BaseModel, ConfigDict, Field, field_validator
from common.models.listing_monitor_task import ListingMonitorTask
from common.models.listing_monitor_reliability import ListingMonitorState, ListingMonitorPage
from common.services.listing_monitor_reliability import monitor_v2_enabled, key, query_key, context as page_identity
from common.utils.xianyu_utils import generate_sign, trans_cookies


class MonitorSearchPage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    task_id: int = Field(gt=0)
    account_id: str = Field(min_length=1, max_length=80)
    keyword: str = Field(min_length=1, max_length=200)
    page: int = Field(ge=1)
    generation: int = Field(ge=1)
    query_key: str = Field(pattern=r'^[0-9a-f]{64}$')
    request_id: str = Field(min_length=1, max_length=96)
    monitor_type: Literal['listing', 'price_drop']
    rows_per_page: Literal[30] = 30
    price_min: str | None = Field(default=None, max_length=32)
    price_max: str | None = Field(default=None, max_length=32)
    publish_days: int | None = Field(default=None, ge=1, le=365)

    @field_validator('price_min', 'price_max')
    @classmethod
    def valid_price(cls, value):
        if value is None: return value
        try:
            price = Decimal(value)
            if not price.is_finite() or not 0 <= price <= Decimal('9999999999.99'):
                raise ValueError('价格范围无效')
        except InvalidOperation as exc: raise ValueError('价格格式无效') from exc
        return value


async def execute_monitor_search_page(context, payload):
    """Register this handler with AccountDispatcher; budget/fence before each request."""
    from common.services.account_dispatch import DispatchError
    from common.services.xianyu_search_client import parse_search_item
    if not monitor_v2_enabled(): raise DispatchError('monitor_v2_disabled')
    request = MonitorSearchPage.model_validate(payload)
    identity = {name: getattr(request, name) for name in
                ('task_id', 'account_id', 'keyword', 'page', 'generation', 'query_key', 'request_id')}
    if request.account_id != context.request.account_id or request.request_id != context.request.request_id:
        raise DispatchError('monitor_context_mismatch')
    async with context.store.sessions() as db:
        task = await db.get(ListingMonitorTask, request.task_id)
        state = await db.get(ListingMonitorState, request.task_id)
        page = await db.get(ListingMonitorPage, key(request.task_id, request.generation, request.page))
        if (not task or task.owner_id != context.request.owner_id or task.is_deleted or not task.is_enabled
                or not state or not page or page_identity(page) != identity
                or state.generation != request.generation or query_key(task, state.region) != request.query_key
                or request.account_id not in (task.account_ids or [])):
            raise DispatchError('monitor_stale_query')
        if task.proxy_url or task.direct_order or task.order_account_ids or task.dm_content:
            raise DispatchError('monitor_configuration_conflict')
        if (request.monitor_type != task.monitor_type or request.publish_days != task.publish_days
                or request.price_min != (str(task.price_min) if task.price_min is not None else None)
                or request.price_max != (str(task.price_max) if task.price_max is not None else None)):
            raise DispatchError('monitor_filter_mismatch')
    account = await context.check()
    session = getattr(context.live, 'session', None)
    if session is None: raise DispatchError('executor_http_unavailable')
    token = trans_cookies(account.cookie or '').get('_m_h5_tk', '').split('_')[0]
    if not token: raise DispatchError('account_token_missing')
    filters = 'quickFilter:filterPersonal;'
    if request.publish_days and request.monitor_type == 'listing':
        filters += f'publishDays:{request.publish_days};'
    # Price filtering is local: collect the complete observation range so rises above
    # the threshold still advance price history before a later legitimate drop.
    data = dict(pageNumber=request.page, keyword=request.keyword, fromFilter=True,
        rowsPerPage=30, sortField='create' if request.monitor_type == 'listing' else 'reduce',
        sortValue='desc', customDistance='', gps='', propValueStr={'searchFilter': filters},
        customGps='', searchReqFromPage='pcSearch', extraFilterValue='{}', userPositionJson='{}')
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    timestamp = str(int(time.time() * 1000))
    api = 'mtop.taobao.idlemtopsearch.pc.search'
    params = dict(jsv='2.7.2', appKey='34839810', t=timestamp, sign=generate_sign(timestamp, token, text),
        v='1.0', type='originaljson', dataType='json', api=api, sessionOption='AutoLoginOnly')
    await context.before_external()
    # Live session inherits the executor's fixed proxy; payload has no URL/proxy/Cookie fields.
    async with session.post(f'https://h5api.m.goofish.com/h5/{api}/1.0/', params=params,
            data={'data': text}, headers={'cookie': account.cookie, 'Referer': 'https://www.goofish.com/'},
            timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False) as response:
        await context.check()
        if response.status == 429:
            from common.services.account_request_budget import parse_retry_after
            hint = parse_retry_after(response.headers.get('Retry-After')) or 60
            await context.budget.defer(request.account_id, hint)
        result = {**identity, 'http_status': response.status, 'ret': [], 'items': None}
        if response.status < 200 or response.status >= 300: return result
        try: body = await response.json()
        except (ValueError, aiohttp.ContentTypeError): return result
        if not isinstance(body, dict): return result
        ret = body.get('ret')
        if not isinstance(ret, list) or not ret or not all(isinstance(r, str) for r in ret): return result
        success = all(r == 'SUCCESS' or r.startswith('SUCCESS::') for r in ret)
        result['ret'] = ['SUCCESS'] if success else ['PLATFORM_REJECTED']
        if not success: return result
        data = body.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('resultList'), list): return result
        items = []
        for raw in data['resultList']:
            try:
                parsed = parse_search_item(raw)
                if not parsed: return result
                items.append({k: parsed.get(k) for k in ('item_id', 'price', 'title', 'area')})
            except (AttributeError, TypeError, ValueError, KeyError): return result
        result['items'] = items
        return result
