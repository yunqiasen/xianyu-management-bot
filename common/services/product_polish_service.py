"""Manual and scheduled polishing share durable daily claims and item results."""
import json
import time
import uuid
from datetime import datetime, timezone
import aiohttp
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from common.models.product_polish_run import ProductPolishRun
from common.models.product_polish_schedule import ProductPolishSchedule
from common.models.xy_catalog_item import XYCatalogItem
from common.models.scheduled_polish_log import ScheduledPolishLog
from common.services.product_polish_schedule import ProductPolishScheduleService, polish_window
from common.services.product_admission import product_admission
from common.utils.xianyu_utils import trans_cookies, generate_sign

class ProductPolishTransport:
    @staticmethod
    async def send(account, item_id):
        from common.services.account_business_client import dispatch_business
        return await dispatch_business(account, 'polish_item', {'item_id':item_id})

class ProductPolishService:
    def __init__(self, session): self.session=session

    async def run(self, account, source='manual', now=None, batch_id=None):
        now=now or datetime.now(timezone.utc)
        batch_id=batch_id or str(uuid.uuid4())
        state=product_admission(account)
        schedule_svc=ProductPolishScheduleService(self.session)
        schedule=await schedule_svc.get(account.owner_id,account.account_id)
        config={k:getattr(schedule,k) for k in ('timezone_name','start','end')} if schedule else {}
        window=polish_window(now,**config)
        cycle=window['cycle']
        if not state['allowed']:
            # Persist deferral but do not consume a cycle during account recovery.
            row=ProductPolishRun(owner_id=account.owner_id,account_id=account.account_id,source=source,
                status=state['status'],result=state)
            self.session.add(row); await self.session.commit()
            return state
        if source=='scheduled':
            admission=await schedule_svc.admit(account,now)
            if admission['status'] not in ('ready','legacy'):
                # No catch-up: the original time window stays authoritative after recovery.
                if schedule and admission['status']=='outside_window':
                    schedule.last_status='skipped_outside_window'; await self.session.commit()
                return admission
        row=ProductPolishRun(owner_id=account.owner_id,account_id=account.account_id,cycle=cycle,
                             source=source,status='unknown',result={'message':'执行中或中断待核实'})
        self.session.add(row)
        try: await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            return dict(status='already_processed',next_run_at=window['next_run_at'])
        items=list((await self.session.execute(select(XYCatalogItem).where(XYCatalogItem.owner_id==account.owner_id,
            XYCatalogItem.account_pk==account.id,XYCatalogItem.is_polished.is_not(True)).order_by(XYCatalogItem.id))).scalars())
        results=[]
        for item in items:
            await self.session.refresh(account)
            admission=product_admission(account)
            if not admission['allowed']:
                results.append(dict(item_id=item.item_id,status='skipped',reason=admission['status'])); break
            try: response=await ProductPolishTransport.send(account,item.item_id)
            except Exception: response={'unknown':True,'message':'擦亮提交中断，先核实'}
            status='success' if response.get('success') else 'failed' if response.get('definitive_failure') else 'unknown'
            if status=='success': item.is_polished=True
            results.append(dict(item_id=item.item_id,status=status,message=response.get('message')))
            self.session.add(ScheduledPolishLog(batch_id=batch_id,account_id=account.account_id,item_id=item.item_id,
                status=status,error_message=None if status=='success' else str(response.get('message',''))[:500]))
            row.result={'items':list(results),'next_run_at':window['next_run_at']}
            await self.session.commit()
            # Stop this account on ambiguous transport/auth/limit failure; no tight-loop retry.
            if status!='success': break
        status='unknown' if any(r['status']=='unknown' for r in results) else 'partial' if any(r['status']!='success' for r in results) else 'success'
        row.status=status; row.result={'items':results,'next_run_at':window['next_run_at']}
        if schedule:
            await self.session.execute(update(ProductPolishSchedule).where(ProductPolishSchedule.id==schedule.id).values(
                last_cycle=cycle,last_status=status))
        await self.session.commit()
        return dict(id=row.id,status=status,**row.result)

    async def history(self,owner_id,account_id):
        rows=(await self.session.execute(select(ProductPolishRun).where(ProductPolishRun.owner_id==owner_id,
            ProductPolishRun.account_id==account_id).order_by(ProductPolishRun.id.desc()).limit(100))).scalars()
        return [dict(id=r.id,cycle=r.cycle,source=r.source,status=r.status,result=r.result,created_at=r.created_at.isoformat()) for r in rows]
