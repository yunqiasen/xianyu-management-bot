"""Persistent queue; Web/WS import the same runner. No automatic replay of unknown sends."""
from copy import deepcopy
import hashlib
import uuid
from datetime import datetime, timedelta
from sqlalchemy import select, update, func
from common.models.product_operation import ProductPublishBatch, ProductOperationEvidence
from common.models.publish_log import PublishLog
from common.models.xy_account import XYAccount
from common.models.xy_catalog_item import XYCatalogItem
from common.utils.time_utils import get_beijing_now_naive

class ProductBatchService:
    def __init__(self, session):
        self.session = session

    async def create(self, owner_id, account_ids, materials, batch_id=None):
        accounts=list(dict.fromkeys(account_ids))
        owned=set((await self.session.execute(select(XYAccount.account_id).where(
            XYAccount.owner_id==owner_id, XYAccount.account_id.in_(accounts)))).scalars())
        if set(accounts)!=owned:
            raise ValueError('账号不存在或无权使用')
        bid=batch_id or str(uuid.uuid4())
        batch=ProductPublishBatch(id=bid,owner_id=owner_id,account_ids=accounts,material_count=len(materials))
        self.session.add(batch)
        for account in accounts:
            for index, material in enumerate(materials):
                self.session.add(PublishLog(user_id=owner_id, account_id=account,
                    title=material['title'], description=material.get('description'), price=str(material.get('price','')),
                    material_id=material.get('id'),batch_id=bid,status='pending',
                    publish_request_id=f'batch:{bid}:{hashlib.sha256(account.encode()).hexdigest()[:12]}:{index}',
                    publish_snapshot=deepcopy(material)))
        self.evidence(owner_id,f'batch:{bid}','created',{'total':len(accounts)*len(materials)})
        await self.session.commit()
        return batch

    def evidence(self, owner_id, target, action, detail):
        self.session.add(ProductOperationEvidence(owner_id=owner_id,actor_id=owner_id,target=target,action=action,detail=detail))

    async def get(self, owner_id, bid):
        return (await self.session.execute(select(ProductPublishBatch).where(
            ProductPublishBatch.id==bid,ProductPublishBatch.owner_id==owner_id))).scalar_one_or_none()

    async def status(self, owner_id, bid):
        batch=await self.get(owner_id,bid)
        if not batch: return None
        logs=list((await self.session.execute(select(PublishLog).where(
            PublishLog.batch_id==bid,PublishLog.user_id==owner_id).order_by(PublishLog.id).execution_options(populate_existing=True))).scalars())
        def counts(rows):
            return {state:sum(row.status==state for row in rows) for state in
                    ('pending','publishing','success','failed','unknown','cancelled')}
        result=counts(logs)
        result.update(batch_id=bid,total=len(logs),finished=not (result['pending']+result['publishing']),
                      cancel_requested=batch.cancelled, account_statuses=[dict(account_id=a,total=sum(l.account_id==a for l in logs),
                      **counts([l for l in logs if l.account_id==a]),sync_status='unknown',
                      sync_message='发布后同步结果见单项日志',sync_total_count=0,sync_saved_count=0) for a in batch.account_ids])
        sync_rows = (await self.session.execute(select(ProductOperationEvidence).where(
            ProductOperationEvidence.owner_id == owner_id, ProductOperationEvidence.target == f'batch:{bid}',
            ProductOperationEvidence.action == 'account_sync').order_by(ProductOperationEvidence.id))).scalars()
        database_now = (await self.session.execute(select(func.now()))).scalar_one()
        sync_map = {}
        for entry in sync_rows:
            info = dict(entry.detail)
            if info.get('sync_status') == 'running' and entry.created_at < database_now - timedelta(minutes=15):
                info.update(sync_status='unknown', sync_message='同步过程已中断或超时，请重新同步商品；不重发商品')
            sync_map[info['account_id']] = info
        for account_status in result['account_statuses']:
            info = sync_map.get(account_status['account_id'])
            if info:
                account_status.update({k: v for k, v in info.items() if k.startswith('sync_')})
            elif not account_status['success'] and not (account_status['pending'] + account_status['publishing']):
                account_status.update(sync_status='skipped', sync_message='没有成功发布项，未触发同步')
        result['finished'] = result['finished'] and all(a['sync_status'] not in ('running', 'pending') for a in result['account_statuses'])
        result['items']=[dict(id=l.id,account_id=l.account_id,title=l.title,status=l.status,
                              item_id=l.item_id,error_message=l.error_message) for l in logs]
        return result

    async def cancel(self, owner_id, bid):
        batch=await self.get(owner_id,bid)
        if not batch: raise ValueError('批次不存在')
        batch.cancelled=True
        changed=await self.session.execute(update(PublishLog).where(PublishLog.batch_id==bid,
            PublishLog.user_id==owner_id,PublishLog.status=='pending').values(status='cancelled'))
        self.evidence(owner_id,f'batch:{bid}','cancel',{'cancelled':changed.rowcount})
        await self.session.commit()
        return changed.rowcount

    async def retry_failed(self, owner_id, bid):
        batch=await self.get(owner_id,bid)
        if not batch: raise ValueError('批次不存在')
        changed=await self.session.execute(update(PublishLog).where(PublishLog.batch_id==bid,
            PublishLog.user_id==owner_id,PublishLog.status=='failed').values(status='pending'))
        self.evidence(owner_id,f'batch:{bid}','retry_failed',{'queued':changed.rowcount})
        await self.session.commit()
        return changed.rowcount

    async def run(self, owner_id, bid, static_root=None):
        from common.services.publish_execution_service import execute_single_publish
        if not await self.get(owner_id,bid): raise ValueError('批次不存在')
        rows=list((await self.session.execute(select(PublishLog).where(PublishLog.batch_id==bid,
            PublishLog.user_id==owner_id,PublishLog.status=='pending').order_by(PublishLog.id))).scalars())
        for row in rows:
            # Single executor atomically claims pending. Cancel and competing runners lose this CAS.
            await execute_single_publish(self.session,owner_id,row.account_id,deepcopy(row.publish_snapshot),
                static_root=static_root,publish_request_id=row.publish_request_id,queued_log_id=row.id)
        return await self.status(owner_id,bid)

    async def reconcile(self, owner_id, log_id, outcome, evidence, item_id=None):
        log=await self.session.get(PublishLog,log_id)
        if not log or log.user_id!=owner_id: raise ValueError('记录不存在')
        if log.source_event_id is not None: raise ValueError('自动续售请使用续售对账入口')
        if log.status not in ('unknown','publishing'): raise ValueError('仅待核实记录支持核对')
        database_now = (await self.session.execute(select(func.now()))).scalar_one()
        if log.status=='publishing' and log.updated_at>database_now-timedelta(minutes=15):
            raise ValueError('发布仍在执行，15分钟后再核对中断记录')
        if outcome=='published':
            if not item_id: raise ValueError('请填写已同步的平台商品ID')
            item=(await self.session.execute(select(XYCatalogItem).where(XYCatalogItem.owner_id==owner_id,
                XYCatalogItem.account_pk.in_(select(XYAccount.id).where(XYAccount.owner_id==owner_id,XYAccount.account_id==log.account_id)),XYCatalogItem.item_id==item_id))).scalar_one_or_none()
            if not item: raise ValueError('商品尚未同步或账号归属不符，请先同步核对')
        from common.utils.xianyu_utils import canonical_goofish_item_url
        changed=await self.session.execute(update(PublishLog).where(PublishLog.id==log_id,
            PublishLog.status==log.status).values(status='success' if outcome=='published' else 'failed',
            item_id=item_id if outcome=='published' else None,
            item_url=canonical_goofish_item_url(item_id) if outcome=='published' else None,error_message='人工核对：'+evidence[:900]))
        if changed.rowcount!=1: raise ValueError('记录已更新，请刷新')
        self.evidence(owner_id,f'publish:{log_id}','reconcile',{'outcome':outcome,'item_id':item_id,'evidence':evidence})
        await self.session.commit()

    async def list_evidence(self, owner_id, target):
        rows=(await self.session.execute(select(ProductOperationEvidence).where(
            ProductOperationEvidence.owner_id==owner_id,ProductOperationEvidence.target==target).order_by(ProductOperationEvidence.id))).scalars()
        return [dict(actor_id=r.actor_id,action=r.action,detail=r.detail,created_at=r.created_at.isoformat()) for r in rows]
