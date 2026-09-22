from datetime import datetime,timedelta
from decimal import Decimal,InvalidOperation
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from sqlalchemy import select,func
from common.models.xy_order import XYOrder

PAID=('paid','pending_ship','pending_shipment','awaiting_shipment','待发货','shipped','completed','已发货','已完成','refunded','退款成功','已退款')
REFUNDED=('refunded','退款成功','已退款')

def amount(value):
    try:
        result=Decimal(str(value or 0))
        if not result.is_finite() or result<0: raise ValueError('金额数据异常')
        return result.quantize(Decimal('0.01'))
    except InvalidOperation: raise ValueError('金额数据异常') from None

async def operating_summary(session,owner,start,end,tz):
    try:
        zone=ZoneInfo(tz); left=datetime.fromisoformat(start); right=datetime.fromisoformat(end)
        left=left.replace(tzinfo=zone) if left.tzinfo is None else left.astimezone(zone)
        right=right.replace(tzinfo=zone) if right.tzinfo is None else right.astimezone(zone)
    except (ValueError,ZoneInfoNotFoundError): raise ValueError('时间或时区格式有误') from None
    if not timedelta(0)<right-left<=timedelta(days=366): raise ValueError('时间窗应在1年至内且结束晚于开始')
    dbzone=ZoneInfo('Asia/Shanghai')
    source_time=XYOrder.placed_at
    rows=(await session.execute(select(XYOrder).where((XYOrder.owner_id==owner) if owner is not None else True,XYOrder.currency=='CNY',XYOrder.status.in_(PAID),source_time>=left.astimezone(dbzone).replace(tzinfo=None),source_time<right.astimezone(dbzone).replace(tzinfo=None)).order_by(source_time,XYOrder.id).limit(10001))).scalars().all()
    if len(rows)>10000: raise ValueError('订单超过10000条，请缩小时间窗')
    details=[]; daily={}; paid=Decimal(0); refund=Decimal(0)
    for row in rows:
        metadata=row.metadata_json or {}
        payment=amount(metadata.get('paid_amount',row.amount))
        refunded=amount(metadata.get('refund_amount',payment if row.status in REFUNDED else 0))
        if refunded>payment: raise ValueError('退款额超过付款额，请核对订单')
        time_=row.placed_at or row.created_at
        day=(time_.replace(tzinfo=dbzone) if time_.tzinfo is None else time_).astimezone(zone).date().isoformat()
        bucket=daily.setdefault(day,{'count':0,'paid':Decimal(0),'refund':Decimal(0)})
        bucket['count']+=1;bucket['paid']+=payment;bucket['refund']+=refunded
        paid+=payment;refund+=refunded
        details.append(dict(id=row.id,order_no=row.order_no,date=day,status=row.status,paid=f'{payment:.2f}',refund=f'{refunded:.2f}',net=f'{payment-refunded:.2f}',amount_source='paid_amount' if 'paid_amount' in metadata else 'paid_status_order_amount'))
    summary=lambda count,p,r:dict(count=count,paid=f'{p:.2f}',refund=f'{r:.2f}',net=f'{p-r:.2f}')
    return dict(summary=summary(len(rows),paid,refund),trend=[dict(date=day,**summary(v['count'],v['paid'],v['refund'])) for day,v in sorted(daily.items())],details=details,business_available_accounts=None,availability_status='requires_execution_state',policy={'timezone':tz,'start':left.isoformat(),'end_exclusive':right.isoformat(),'time_basis':'placed_at下单时间；缺下单时间不纳入，避免历史同步算入今天（非现金到账时间）','paid_statuses':PAID,'currency':'CNY','refund_basis':'明确refund_amount；整单退款状态缺明细时按整单金额','amount_basis':'paid_amount优先，已付款状态缺明细时使用订单金额'})
