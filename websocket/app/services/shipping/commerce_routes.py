"""持久履约的内部推进入口；只补确认从不调用内容发送。"""
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from app.api.deps import require_internal_auth
from common.db.session import async_session_maker
from common.models.delivery_intent import DeliveryIntent
from common.models.xy_order import XYOrder
from common.services.delivery_execution import DeliveryExecution
from common.services.delivery_transport import send_payload
from common.services.card_purchase import purchase_card

router=APIRouter(prefix='/internal/orders',tags=['internal'],dependencies=[Depends(require_internal_auth)])
class AdvanceRequest(BaseModel):
    intent_id: str
    owner_id: int
    confirm_only: bool=True
    query_supplier: bool=False

@router.post('/fulfillment')
async def advance_fulfillment(body:AdvanceRequest):
    from app.services.xianyu.cookie_manager import get_manager
    async with async_session_maker() as s:
        row=await s.scalar(select(DeliveryIntent).where(DeliveryIntent.id==body.intent_id,DeliveryIntent.owner_id==body.owner_id))
        if not row: raise HTTPException(404,'履约记录不存在')
        order=await s.scalar(select(XYOrder).where(XYOrder.owner_id==row.owner_id,XYOrder.account_id==row.account_id,XYOrder.order_no==row.order_no))
        if not order: raise HTTPException(404,'订单不存在')
    live=get_manager().instances.get(row.account_id)
    if not live or not live.ws: raise HTTPException(409,'账号未连接')
    if body.query_supplier:
        from common.services.card_purchase import query_card
        async def query(config,**kwargs):
            return await query_card(config,connector=live._build_session_connector(),**kwargs)
        try:
            result=await DeliveryExecution(async_session_maker).verify_supplier(row.id,row.owner_id,query=query,check=live._check_account_execution)
            return {'success':True,'data':{'id':result.id,'content_state':result.content_state}}
        except Exception: raise HTTPException(409,'供应商证据待核实；未重复采购')
    if body.confirm_only and row.content_state!='confirmed': raise HTTPException(409,'内容尚未确认，请先核实')
    handler=live.auto_delivery_handler
    if not body.confirm_only:
        check=await handler.pre_delivery_check_and_close(websocket=live.ws,order_no=order.order_no,buyer_id=order.buyer_id,chat_id=order.chat_id,item_id=order.item_id)
        if check.get('action')!='allow': raise HTTPException(409,'发货规则已暂停此订单')
    async def send(payload):
        return await send_payload(payload,lambda text:handler.send_msg(live.ws,order.chat_id,order.buyer_id,text),lambda image:handler.send_image_msg(live.ws,order.chat_id,order.buyer_id,image),record=lambda part,state:DeliveryExecution(async_session_maker).record_content(row.id,row.owner_id,part,state))
    async def confirm(number):
        from app.services.shipping.commerce_confirmation import confirm_order
        return await confirm_order(DeliveryExecution(async_session_maker),row,order,handler,live._check_account_execution)
    async def purchase(config, **kwargs):
        return await purchase_card(config, connector=live._build_session_connector(), **kwargs)
    result=await DeliveryExecution(async_session_maker).execute(row.id,row.owner_id,
        send=None if body.confirm_only else send,
        confirm=confirm if handler.is_auto_confirm_enabled() else None,
        purchase=None if body.confirm_only else purchase, check=live._check_account_execution)
    return {'success':True,'data':{'id':result.id,'content_state':result.content_state,'confirm_state':result.confirm_state}}

class StartRequest(BaseModel):
    owner_id: int
    account_id: str
    order_no: str
    card_id: int | None = None

@router.post('/fulfillment/start')
async def start_fulfillment(body:StartRequest):
    from types import SimpleNamespace
    from app.services.shipping.legacy_bridge import maybe_deliver
    async with async_session_maker() as s:
        orders=(await s.scalars(select(XYOrder).where(XYOrder.owner_id==body.owner_id,XYOrder.account_id==body.account_id,XYOrder.order_no==body.order_no))).all()
        if len(orders)!=1: raise HTTPException(409 if orders else 404,'订单身份不唯一' if orders else '订单不存在')
        order=orders[0]
    result=await maybe_deliver(SimpleNamespace(**body.model_dump(),item_id=order.item_id,buyer_id=order.buyer_id,chat_id=order.chat_id))
    if result is None: return {'success':False,'code':409,'message':'此卡券使用原专用发货入口','data':None}
    return result

from app.services.shipping.order_platform_routes import router as platform_router
router.include_router(platform_router)
