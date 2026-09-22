"""订单页的历史任务、阶段事实、核实及显式人工补发。"""
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException,Query
from pydantic import BaseModel,Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.api import deps
from common.models.delivery_intent import DeliveryIntent
from common.models.order_sync_job import OrderSyncJob
from common.services.delivery_execution import DeliveryExecution
from common.services.order_history import OrderHistory
from common.services.order_delivery_runtime import OrderDeliveryRuntime

router=APIRouter(prefix='/commerce',tags=['orders'])

def sessions(db): return async_sessionmaker(db.bind,expire_on_commit=False)
def intent_view(row):
    view = {key:getattr(row,key) for key in ('id','account_id','order_no','line_id','operation_key','card_type','quantity','mode','content_state','confirm_state','confirm_attempts','next_retry_at','not_before','last_error')}
    source=row.source_snapshot or {}
    view['source']={key:source.get(key) for key in ('card_name','card_updated_at','card_source','spec_name','spec_value','order_quantity','line_amount','rule_id','rule_version')}
    view['pieces']=[event for event in row.evidence or [] if event.get('phase')=='content_piece']
    view['evidence']=[event for event in row.evidence or [] if event.get('phase')!='content_piece']
    view['confirm_attempt_limit']=4
    return view
def job_view(row):
    return {key:getattr(row,key) for key in ('id','account_id','status','next_page','total_pages','imported','last_error')}
async def owned_intent(db,owner,intent_id):
    row=await db.scalar(select(DeliveryIntent).where(DeliveryIntent.owner_id==owner,DeliveryIntent.id==intent_id))
    if not row: raise HTTPException(404,'履约记录不存在')
    return row
async def domain(call):
    try: return await call
    except PermissionError: raise HTTPException(404,'记录不存在')
    except ValueError as e: raise HTTPException(409,str(e))

class HistoryRequest(BaseModel): account_id: str=Field(min_length=1,max_length=64)
class EvidenceRequest(BaseModel):
    phase: Literal['content','confirm']
    result: Literal['confirmed','not_sent']
    evidence: str=Field(min_length=1,max_length=120,pattern=r'^[\w.:/-]+$')
class ResendRequest(BaseModel):
    reason: str=Field(min_length=1,max_length=200)
    acknowledged: bool
    request_id: str=Field(min_length=8,max_length=40,pattern=r'^[A-Za-z0-9_-]+$')

@router.get('/history')
async def history_list(account_id:str|None=None,user=Depends(deps.get_current_active_user),db=Depends(deps.get_db_session)):
    stmt=select(OrderSyncJob).where(OrderSyncJob.owner_id==user.id)
    if account_id: stmt=stmt.where(OrderSyncJob.account_id==account_id)
    rows=(await db.scalars(stmt.order_by(OrderSyncJob.created_at.desc()).limit(50))).all()
    return {'success':True,'data':[job_view(r) for r in rows]}

@router.post('/history')
async def history_create(body:HistoryRequest,user=Depends(deps.get_current_active_user),db=Depends(deps.get_db_session)):
    row=await domain(OrderHistory(sessions(db)).create(user.id,body.account_id))
    return {'success':True,'data':job_view(row)}

@router.post('/history/{job_id}/{action}')
async def history_action(job_id:str,action:Literal['step','cancel','resume'],user=Depends(deps.get_current_active_user),db=Depends(deps.get_db_session)):
    row=await domain(getattr(OrderHistory(sessions(db)),action)(user.id,job_id))
    return {'success':True,'data':job_view(row)}

@router.get('/intents')
async def intent_list(account_id:str|None=None,user=Depends(deps.get_current_active_user),db=Depends(deps.get_db_session)):
    stmt=select(DeliveryIntent).where(DeliveryIntent.owner_id==user.id)
    if account_id: stmt=stmt.where(DeliveryIntent.account_id==account_id)
    rows=(await db.scalars(stmt.order_by(DeliveryIntent.created_at.desc()).limit(100))).all()
    return {'success':True,'data':[intent_view(r) for r in rows]}

@router.get('/intents/{intent_id}/content')
async def intent_content(intent_id:str,user=Depends(deps.get_current_active_user),db=Depends(deps.get_db_session)):
    row=await owned_intent(db,user.id,intent_id)
    return {'success':True,'data':row.payload}

@router.post('/intents/{intent_id}/reconcile')
async def reconcile(intent_id:str,body:EvidenceRequest,user=Depends(deps.get_current_active_user),db=Depends(deps.get_db_session)):
    row=await domain(DeliveryExecution(sessions(db)).reconcile(intent_id,user.id,body.phase,body.result,evidence=body.evidence))
    return {'success':True,'data':intent_view(row)}

@router.get('/rule-preview')
async def preview(account_id:str,order_no:str,user=Depends(deps.get_current_active_user),db=Depends(deps.get_db_session)):
    rows=await domain(OrderDeliveryRuntime(sessions(db)).preview(user.id,account_id,order_no))
    return {'success':True,'data':rows}

async def dispatch(intent_id,owner,*,confirm_only):
    from app.services.websocket_client import websocket_client
    try:
        return await websocket_client.http_client.post(websocket_client.base_url+'/internal/orders/fulfillment',json={'intent_id':intent_id,'owner_id':owner,'confirm_only':confirm_only},max_retries=1)
    except Exception: raise HTTPException(503,'履约服务暂未响应；保留当前阶段，请刷新核实')

@router.post('/intents/{intent_id}/confirm')
async def only_confirm(intent_id:str,user=Depends(deps.get_current_active_user),db=Depends(deps.get_db_session)):
    row=await owned_intent(db,user.id,intent_id)
    if row.content_state!='confirmed' or row.confirm_state!='pending': raise HTTPException(409,'当前阶段需要先核实或不需要确认')
    return await dispatch(intent_id,user.id,confirm_only=True)

@router.post('/intents/{intent_id}/resend')
async def resend(intent_id:str,body:ResendRequest,user=Depends(deps.get_current_active_user),db=Depends(deps.get_db_session)):
    row=await domain(DeliveryExecution(sessions(db)).resend(intent_id,user.id,request_id=body.request_id,acknowledged=body.acknowledged,reason=body.reason))
    # 先提交新意图，再发送；HTTP超时用相同request_id重试，避免再次出库。
    return await dispatch(row.id,user.id,confirm_only=False)


@router.post('/intents/{intent_id}/advance')
async def continue_delivery(intent_id:str,user=Depends(deps.get_current_active_user),db=Depends(deps.get_db_session)):
    row=await owned_intent(db,user.id,intent_id)
    if row.content_state!='reserved': raise HTTPException(409,'此阶段应先核实或仅补确认')
    return await dispatch(row.id,user.id,confirm_only=False)

@router.post('/intents/{intent_id}/query-supplier')
async def verify_supplier(intent_id:str,user=Depends(deps.get_current_active_user),db=Depends(deps.get_db_session)):
    row=await owned_intent(db,user.id,intent_id)
    from app.services.websocket_client import websocket_client
    try:
        return await websocket_client.http_client.post(websocket_client.base_url+'/internal/orders/fulfillment',json={'intent_id':row.id,'owner_id':user.id,'query_supplier':True})
    except Exception: raise HTTPException(409,'供应商查询未取得完整证据；本次未重复采购')

class SupplierEvidenceRequest(BaseModel):
    texts: list[str]=Field(min_length=1,max_length=1000)
    evidence: str=Field(min_length=1,max_length=120,pattern=r'^[\w.:/-]+$')

@router.post('/intents/{intent_id}/supplier-evidence')
async def supplier_evidence(intent_id:str,body:SupplierEvidenceRequest,user=Depends(deps.get_current_active_user),db=Depends(deps.get_db_session)):
    await owned_intent(db,user.id,intent_id)
    if sum(map(len,body.texts))>256000: raise HTTPException(413,'采购内容过大')
    row=await domain(DeliveryExecution(sessions(db)).reconcile_purchase(intent_id,user.id,texts=body.texts,evidence=body.evidence))
    return {'success':True,'data':intent_view(row)}

# Dynamic title rules are configuration only; a rule never performs a send itself.
from pydantic import ConfigDict
from common.services.delivery_rules import DeliveryRules

class DeliveryRuleCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    card_id: int = Field(gt=0)
    account_id: str | None = Field(default=None, min_length=1, max_length=80)
    keyword: str = Field(min_length=1, max_length=255)
    match_mode: Literal['contains','exact','legacy_contains'] = 'contains'
    delivery_count: int = Field(default=1, ge=1, le=1000)
    enabled: bool = False
    priority: int = Field(default=0, ge=-1000, le=1000)
    description: str | None = Field(default=None, max_length=1000)

class DeliveryRuleUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_version: int = Field(ge=1)
    card_id: int | None = Field(default=None, gt=0)
    account_id: str | None = Field(default=None, min_length=1, max_length=80)
    keyword: str | None = Field(default=None, min_length=1, max_length=255)
    match_mode: Literal['contains','exact','legacy_contains'] | None = None
    delivery_count: int | None = Field(default=None, ge=1, le=1000)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=-1000, le=1000)
    description: str | None = Field(default=None, max_length=1000)

@router.get('/rules')
async def delivery_rule_list(account_id:str|None=None, user=Depends(deps.get_current_active_user), db=Depends(deps.get_db_session)):
    return {'success':True, 'data':await domain(DeliveryRules(sessions(db)).list(user.id, account_id))}

@router.post('/rules')
async def delivery_rule_create(body:DeliveryRuleCreate, user=Depends(deps.get_current_active_user), db=Depends(deps.get_db_session)):
    return {'success':True, 'data':await domain(DeliveryRules(sessions(db)).save(user.id, body.model_dump()))}

@router.put('/rules/{rule_id}')
async def delivery_rule_update(rule_id:str, body:DeliveryRuleUpdate, user=Depends(deps.get_current_active_user), db=Depends(deps.get_db_session)):
    data=body.model_dump(exclude_unset=True, exclude={'expected_version'})
    if any(value is None for key,value in data.items() if key not in {'account_id','description'}):
        raise HTTPException(422,'规则字段应提供明确值')
    return {'success':True, 'data':await domain(DeliveryRules(sessions(db)).save(user.id, data, rule_id=rule_id, expected_version=body.expected_version))}


@router.get('/order-lines')
async def order_line_list(account_id:str, order_no:str, user=Depends(deps.get_current_active_user), db=Depends(deps.get_db_session)):
    from common.models.xy_order import XYOrder
    from common.services.order_lines import order_lines
    from dataclasses import asdict
    order=await db.scalar(select(XYOrder).where(XYOrder.owner_id==user.id, XYOrder.account_id==account_id, XYOrder.order_no==order_no))
    if order is None: raise HTTPException(404,'订单不存在')
    try: lines=[asdict(line) for line in order_lines(order)]
    except ValueError: raise HTTPException(409,'订单明细待核实')
    return {'success':True,'data':lines}
