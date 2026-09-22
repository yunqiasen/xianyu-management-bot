"""可续跑订单历史。一次请求一页；取消/旧版本回包不推进检查点。"""
import time
from sqlalchemy import select
from common.models.order_sync_job import OrderSyncJob
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.services.order_state import merge_order_status
from common.services.account_policy import snapshot
from common.services.order_lines import canonical_lines

class OrderHistory:
    def __init__(self,sessions): self.sessions=sessions
    async def _job(self,s,owner,job_id):
        row=await s.scalar(select(OrderSyncJob).where(OrderSyncJob.id==job_id,OrderSyncJob.owner_id==owner).with_for_update())
        if not row: raise PermissionError('同步任务不存在')
        return row
    async def _account(self,s,owner,account_id):
        row=await s.scalar(select(XYAccount).where(XYAccount.owner_id==owner,XYAccount.account_id==account_id).with_for_update())
        if not row: raise PermissionError('账号不存在')
        return row
    async def create(self,owner,account_id):
        async with self.sessions() as s,s.begin():
            await self._account(s,owner,account_id)
            row=await s.scalar(select(OrderSyncJob).where(OrderSyncJob.owner_id==owner,OrderSyncJob.account_id==account_id,OrderSyncJob.status.in_(['pending','running','paused'])).order_by(OrderSyncJob.created_at.desc()))
            if row: return row
            row=OrderSyncJob(owner_id=owner,account_id=account_id); s.add(row);await s.flush();return row
    async def cancel(self,owner,job_id):
        async with self.sessions() as s,s.begin():
            row=await self._job(s,owner,job_id)
            if row.status!='completed': row.status='cancelled';row.revision+=1
            return row
    async def resume(self,owner,job_id):
        async with self.sessions() as s,s.begin():
            row=await self._job(s,owner,job_id)
            if row.status=='running' and row.lease_until>time.time(): raise ValueError('当前页仍在处理')
            if row.status!='completed': row.status='pending';row.revision+=1;row.last_error=None
            return row
    async def fetch_platform_page(self,account,page):
        from common.services.order_service import OrderService
        async with self.sessions() as s:
            service=OrderService(s)
            data=await service._fetch_sold_orders_page(account.cookie,page,account_id=account.account_id,query_code='ALL')
            if not data or data.get('error'): raise ValueError('订单分页拉取失败')
            orders=[service._parse_sold_order_item(item) for item in data.get('items',[])]
            if any(not o for o in orders): raise ValueError('订单数据解析失败')
            return {'orders':orders,'has_next':data.get('next_page',False),'total_pages':max(1,(data.get('total_count',0)+service._XIANYU_ORDER_PAGE_SIZE-1)//service._XIANYU_ORDER_PAGE_SIZE)}
    async def step(self,owner,job_id,*,fetch=None,now=None):
        now=time.time() if now is None else now
        async with self.sessions() as s,s.begin():
            row=await self._job(s,owner,job_id)
            if row.status not in {'pending','running'} or (row.status=='running' and row.lease_until>now): return row
            account=await self._account(s,owner,row.account_id)
            version=snapshot(account)
            if account.status in {'disabled','inactive','suspended'} or version['business_state'] in {'paused','proxy_error','verification_required'}:
                row.status='paused';row.last_error='account_not_ready';return row
            row.status='running';row.revision+=1;row.lease_until=int(now+120)
            revision=row.revision;page=row.next_page
        try:
            data=await (fetch or self.fetch_platform_page)(account,page)
            if not isinstance(data,dict) or not isinstance(data.get('orders'),list): raise ValueError('分页结构错误')
            for parsed in data['orders']:
                if parsed.get('order_lines') is not None: parsed['order_lines']=canonical_lines(parsed['order_lines'])
        except Exception:
            async with self.sessions() as s,s.begin():
                row=await self._job(s,owner,job_id)
                if row.revision==revision: row.status='paused';row.last_error='page_fetch_failed'
                return row
        async with self.sessions() as s,s.begin():
            row=await self._job(s,owner,job_id)
            if row.status!='running' or row.revision!=revision: return row
            account=await self._account(s,owner,row.account_id);current=snapshot(account)
            if any(current[k]!=version[k] for k in ('generation','credential_version','config_version')):
                row.status='paused';row.last_error='account_version_changed';return row
            # 同一账号行锁串行导入；重复页只更新已有订单。
            for parsed in data['orders']:
                if not parsed.get('order_no'): raise ValueError('订单身份缺失')
                order=await s.scalar(select(XYOrder).where(XYOrder.owner_id==owner,XYOrder.account_id==row.account_id,XYOrder.order_no==parsed['order_no']).with_for_update())
                if not order:
                    order=XYOrder(owner_id=owner,account_id=row.account_id,order_no=parsed['order_no'],status=parsed.get('status') or 'unknown',source='fetch_xianyu');s.add(order)
                else:
                    for k,v in merge_order_status(order,parsed.get('status')).items(): setattr(order,k,v)
                for key in ('item_id','buyer_id','buyer_nick','spec_name','spec_value','quantity','amount','placed_at','receiver_name','receiver_phone','receiver_address'):
                    if parsed.get(key) is not None: setattr(order,key,parsed[key])
                if parsed.get('order_lines') is not None:
                    metadata=dict(order.metadata_json or {})
                    if not metadata.get('status_conflict'):
                        metadata['order_lines']=parsed['order_lines']; order.metadata_json=metadata
                await s.flush()
            row.imported+=len(data['orders']);row.next_page=page+1;row.total_pages=data.get('total_pages',row.total_pages)
            row.status='pending' if data.get('has_next') else 'completed';row.last_error=None
            return row
