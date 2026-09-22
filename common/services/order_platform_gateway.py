"""订单只读 RPC。web/scheduler 不持有平台会话，不发送调用方 Cookie。"""
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from common.db.session import async_session_maker
from common.models.xy_account import XYAccount
from common.services.account_policy import snapshot
from common.services.account_dispatch import AccountDispatchClient

class OrderReadRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    owner_id:int
    account_id:str=Field(min_length=1,max_length=64)
    generation:int
    credential_version:int
    config_version:int
    operation:Literal['sold','refund','detail']
    page:int=Field(default=1,ge=1,le=100000)
    query_code:Literal['ALL','NOT_SHIP']='ALL'
    dispute_status:Literal['1','2','3','5']='1'
    order_no:str=Field(default='',max_length=64)

async def read_order_platform(account_id,operation,**params):
    from app.core.config import get_settings
    if not account_id: raise ValueError('订单读取缺少账号身份')
    async with async_session_maker() as session:
        accounts=(await session.scalars(select(XYAccount).where(XYAccount.account_id==account_id))).all()
        if len(accounts)!=1: raise ValueError('账号身份不唯一')
        account=accounts[0];state=snapshot(account)
    body=OrderReadRequest(owner_id=account.owner_id,account_id=account_id,operation=operation,
        **{key:state[key] for key in ('generation','credential_version','config_version')},**params)
    settings=get_settings()
    client=AccountDispatchClient(settings.websocket_service_url,settings.internal_api_token,timeout=30)
    response=await client._call('POST','/internal/orders/platform-read',json=body.model_dump())
    if response.status_code!=200: raise ValueError('执行方订单读取失败，未在本地重试')
    result=response.json()
    if not result.get('success'): raise ValueError('执行方订单读取待核实')
    return result['data']
