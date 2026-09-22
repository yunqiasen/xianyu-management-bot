"""订单读取只在账号执行方外发，使用实时凭据及固定代理。"""
import json
import time
import aiohttp
from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from common.db.session import async_session_maker
from common.models.xy_account import XYAccount
from common.services.account_policy import snapshot
from common.services.order_platform_gateway import OrderReadRequest
from common.utils.xianyu_utils import trans_cookies, generate_sign

router=APIRouter()
PLATFORM_BASE='https://h5api.m.goofish.com/h5'

async def take_budget(account):
    from common.services.account_request_budget import AccountRequestBudget,RequestBudgetPolicy,account_risk_config
    from common.db.redis_client import get_redis_client
    redis=await get_redis_client()
    await AccountRequestBudget(redis).take(account.account_id,RequestBudgetPolicy.from_risk_config(account_risk_config(account)))

async def checked_account(body,live):
    async with async_session_maker() as session:
        account=await session.scalar(select(XYAccount).where(XYAccount.owner_id==body.owner_id,XYAccount.account_id==body.account_id))
        if not account: raise HTTPException(404,'账号不存在')
        state=snapshot(account)
        if any(state[key]!=getattr(body,key) for key in ('generation','credential_version','config_version')):
            raise HTTPException(409,'账号执行版本已变化')
    await live._check_account_execution()
    runtime=getattr(live,'_account_runtime',None)
    if runtime is None or (runtime.owner_id,runtime.account_id)!=(body.owner_id,body.account_id):
        raise HTTPException(409,'账号执行身份不匹配')
    return account

@router.post('/platform-read')
async def order_platform_read(body:OrderReadRequest):
    from app.services.xianyu.cookie_manager import get_manager
    live=get_manager().instances.get(body.account_id)
    if not live: raise HTTPException(409,'账号执行方未就绪')
    account=await checked_account(body,live)
    if body.operation=='detail':
        if not body.order_no: raise HTTPException(422,'缺少订单号')
        api='mtop.idle.web.trade.order.detail';data={'tid':body.order_no}
    elif body.operation=='sold':
        api='mtop.taobao.idle.trade.merchant.sold.get'
        from common.services.order_service import OrderService
        data={'pageNumber':body.page,'rowsPerPage':OrderService._XIANYU_ORDER_PAGE_SIZE,'orderIds':'','queryCode':body.query_code,'orderSearchParam':'{}'}
    else:
        api='mtop.taobao.idle.merchant.refund.list'
        data={'pageNumber':1,'rowsPerPage':20,'queryType':'refund','refundSearchParam':{'disputeStatus':body.dispute_status,'queryCode':'ALL'}}
    await take_budget(account)
    await checked_account(body,live)
    cookie=live.cookies_str
    token=trans_cookies(cookie).get('_m_h5_tk','').split('_')[0]
    timestamp=str(int(time.time()*1000));raw=json.dumps(data,separators=(',',':'))
    params={'jsv':'2.7.2','appKey':'34839810','t':timestamp,'sign':generate_sign(timestamp,token,raw),
            'v':'1.0','type':'originaljson' if body.operation=='sold' else 'json','accountSite':'xianyu',
            'dataType':'json','api':api,'sessionOption':'AutoLoginOnly'}
    origin='https://seller.goofish.com' if body.operation!='detail' else 'https://www.goofish.com'
    headers={'cookie':cookie,'origin':origin,'referer':origin+'/', 'content-type':'application/x-www-form-urlencoded',
             'user-agent':'Mozilla/5.0 Chrome/138.0.0.0 Safari/537.36'}
    # 构建固定代理失败即退出，没有直连回退，也没有HTTP层重试。
    connector=live._build_session_connector()
    async with aiohttp.ClientSession(connector=connector,cookie_jar=aiohttp.DummyCookieJar(),trust_env=False) as client:
        async with client.post(f'{PLATFORM_BASE}/{api}/1.0/',params=params,data={'data':raw},headers=headers,
                               timeout=aiohttp.ClientTimeout(total=20),allow_redirects=False) as response:
            if response.status!=200: raise HTTPException(409,'平台订单读取待核实')
            result=await response.json(content_type=None)
    await checked_account(body,live)
    if not any('SUCCESS' in str(ret) for ret in result.get('ret',[])):
        raise HTTPException(409,'账号会话或订单读取待核实')
    # Cookie由账号域恢复流程更新，此读取端不写旧凭据，也不触发旧密码登录旁路。
    return {'success':True,'data':result}
