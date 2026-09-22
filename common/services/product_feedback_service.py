"""Durable at-most-once submission per owner/account/order/action; unknown never auto-retries."""
from datetime import timedelta
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from common.models.product_feedback import ProductFeedbackAttempt
from common.models.product_operation import ProductOperationEvidence
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.models.auto_rate_config import AutoRateConfig
from common.services.product_feedback_policy import feedback_eligibility
from common.services.product_admission import product_admission
from common.utils.time_utils import get_beijing_now_naive

class ProductFeedbackService:
    def __init__(self, session): self.session=session

    async def record(self, account, order_no, kind, status, reason=None):
        result=dict(account_id=account.account_id,order_no=order_no,kind=kind,status=status,
                    reason=reason,success=status=='success',message=reason or status,unknown=status=='unknown')
        self.session.add(ProductOperationEvidence(owner_id=account.owner_id,actor_id=account.owner_id,
            target=f'feedback:{account.account_id}',action=kind,detail=result))
        await self.session.commit()
        return result

    async def execute(self, account, order, kind, send):
        eligibility=feedback_eligibility(account,order,kind=kind)
        admission=product_admission(account)
        if not admission['allowed']: eligibility=admission['status']
        if eligibility!='ready': return await self.record(account,order.order_no,kind,'skipped',eligibility)
        if kind=='rate':
            config=(await self.session.execute(select(AutoRateConfig).where(AutoRateConfig.account_id==account.account_id))).scalar_one_or_none()
            if not config or not config.enabled:
                return await self.record(account,order.order_no,kind,'skipped','disabled')
        elif not account.auto_red_flower:
            return await self.record(account,order.order_no,kind,'skipped','disabled')
        query=select(ProductFeedbackAttempt).where(ProductFeedbackAttempt.owner_id==account.owner_id,
            ProductFeedbackAttempt.account_id==account.account_id,ProductFeedbackAttempt.order_no==order.order_no,
            ProductFeedbackAttempt.kind==kind)
        attempt=(await self.session.execute(query)).scalar_one_or_none()
        now=get_beijing_now_naive()
        if attempt:
            reason='already_processed' if attempt.status=='success' else 'unknown' if attempt.status=='unknown' else 'cooldown'
            if attempt.status!='failed' or (attempt.retry_at and attempt.retry_at>now):
                return await self.record(account,order.order_no,kind,'skipped',reason)
            result=await self.session.execute(update(ProductFeedbackAttempt).where(ProductFeedbackAttempt.id==attempt.id,
                ProductFeedbackAttempt.status=='failed',ProductFeedbackAttempt.retry_at<=now).values(status='unknown',retry_at=None))
            await self.session.commit()
            if result.rowcount!=1: return await self.record(account,order.order_no,kind,'skipped','duplicate_plan')
        else:
            attempt=ProductFeedbackAttempt(owner_id=account.owner_id,account_id=account.account_id,order_no=order.order_no,
                                           kind=kind,status='unknown')
            self.session.add(attempt)
            try: await self.session.commit()
            except IntegrityError:
                await self.session.rollback()
                return await self.record(account,order.order_no,kind,'skipped','duplicate_plan')
        try:
            response=await send()
        except Exception:
            response={'success':False,'unknown':True,'message':'提交中断，结果待核实'}
        if response.get('success'):
            state='success'
        elif response.get('definitive_failure'):
            state='failed'
        else:
            state='unknown'
        attempt.status=state
        attempt.retry_at=now+timedelta(seconds=600) if state=='failed' else None
        if state=='success':
            await self.session.execute(update(XYOrder).where(XYOrder.id==order.id,XYOrder.owner_id==account.owner_id,
                XYOrder.account_id==account.account_id).values(**{'is_rated' if kind=='rate' else 'is_red_flower':True}))
        await self.session.commit()
        return await self.record(account,order.order_no,kind,state,str(response.get('message') or state)[:500])

    async def run_order(self, account, order, kind, feedback=None):
        if kind=='rate':
            from common.services.rate_service import RateService, get_rate_feedback_content
            if feedback is None:
                config=(await self.session.execute(select(AutoRateConfig).where(AutoRateConfig.account_id==account.account_id))).scalar_one_or_none()
                feedback=config.text_content if config and config.rate_type=='text' else await get_rate_feedback_content(account.account_id)
            if not feedback: return await self.record(account,order.order_no,kind,'skipped','missing_template')
            send=lambda:RateService(account.cookie,account.account_id)._rate_buyer_impl(order.order_no,feedback)
        else:
            from common.services.product_red_flower import request_red_flower
            send=lambda:request_red_flower(account,order.order_no)
        return await self.execute(account,order,kind,send)

    async def history(self, owner_id, account_id):
        rows=(await self.session.execute(select(ProductOperationEvidence).where(ProductOperationEvidence.owner_id==owner_id,
            ProductOperationEvidence.target==f'feedback:{account_id}').order_by(ProductOperationEvidence.id.desc()).limit(200))).scalars()
        return [dict(id=r.id,created_at=r.created_at.isoformat(),**r.detail) for r in rows]

async def guarded_rate(service, order_no, feedback):
    from common.db.session import async_session_maker
    async with async_session_maker() as session:
        account=(await session.execute(select(XYAccount).where(XYAccount.account_id==service.account_id))).scalar_one_or_none()
        if not account: return {'success':False,'status':'skipped','message':'账号不存在'}
        order=(await session.execute(select(XYOrder).where(XYOrder.owner_id==account.owner_id,
            XYOrder.account_id==account.account_id,XYOrder.order_no==order_no))).scalar_one_or_none()
        if not order: return {'success':False,'status':'skipped','message':'请先同步该账号订单'}
        return await ProductFeedbackService(session).execute(account,order,'rate',lambda:service._rate_buyer_impl(order_no,feedback))
