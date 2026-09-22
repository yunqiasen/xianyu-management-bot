"""订单级持久履约。所有外发前提交状态；未取得证据时不重复发送/购买。

MySQL 下预占按账号->订单->卡券、阶段按意图->账号加行锁；回执提交前重验账号版本。
外部回调只返回 confirmed / not_sent / unknown，禁止把提交当确认。
"""
from __future__ import annotations
import json
import time
from sqlalchemy import select
from common.models.card import Card
from common.models.delivery_intent import DeliveryIntent
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.services.account_policy import snapshot
from common.services.delivery_utils import process_delivery_content_with_description
from common.services.delivery_rules import sku_matches, title_score, delivery_quantity
from common.models.delivery_rule import DeliveryRule
from common.services.order_lines import order_lines

CONFIRM_RETRY_DELAYS = (60, 300, 900)


class DeliveryExecution:
    def __init__(self, sessions): self.sessions=sessions

    async def _account(self,s,owner,account_id):
        row=(await s.execute(select(XYAccount).where(XYAccount.owner_id==owner,XYAccount.account_id==account_id).with_for_update())).scalar_one_or_none()
        if not row: raise PermissionError('账号不存在')
        return row

    @staticmethod
    def _current(account,intent):
        state=snapshot(account)
        return (account.status not in {'inactive','disabled','suspended','deleted'} and
                state['business_state'] not in {'paused','proxy_error','verification_required','disabled'} and
                all(state[k]==getattr(intent,k) for k in ('generation','credential_version','config_version')))

    async def _intent(self,s,intent_id,owner):
        row=(await s.execute(select(DeliveryIntent).where(DeliveryIntent.id==intent_id,DeliveryIntent.owner_id==owner).with_for_update())).scalar_one_or_none()
        if not row: raise PermissionError('履约记录不存在')
        return row

    async def reserve(self,owner,account_id,order_no,card_id,*,mode='send_first',operation_key='payment',allow_closed=False,allow_resend=False,resend_reason=None,rule_id=None,rule_version=None,line_id='main',quantity_override=None):
        async with self.sessions() as s, s.begin():
            account=await self._account(s,owner,account_id)
            order=await self._order(s,owner,account_id,order_no)
            lines=order_lines(order)
            line=next((line for line in lines if line.line_id==line_id),None)
            if line is None: raise ValueError('订单明细已变化')
            if len(lines)>1 and not allow_resend: raise ValueError('多明细订单应整批预占')
            return await self._reserve_row(s,account,order,line,card_id,mode=mode,operation_key=operation_key,
                allow_closed=allow_closed,allow_resend=allow_resend,resend_reason=resend_reason,
                rule_id=rule_id,rule_version=rule_version,quantity_override=quantity_override,
                line_ids=[line.line_id],confirmation_key=operation_key)

    async def _order(self,s,owner,account_id,order_no):
        order=await s.scalar(select(XYOrder).where(XYOrder.owner_id==owner,XYOrder.account_id==account_id,XYOrder.order_no==order_no).with_for_update())
        if order is None: raise PermissionError('订单不存在')
        return order

    async def reserve_batch(self,owner,account_id,order_no,selections,*,mode='send_first',allow_closed=False):
        async with self.sessions() as s,s.begin():
            account=await self._account(s,owner,account_id)
            order=await self._order(s,owner,account_id,order_no)
            lines=order_lines(order)
            by_line={row.get('line_id','main'):row for row in selections}
            if len(by_line)!=len(selections) or set(by_line)!={line.line_id for line in lines}:
                raise ValueError('每个订单明细需要唯一匹配规则')
            if allow_closed and len(lines)>1: raise ValueError('关闭订单的多明细需逐项人工核实')
            output=[]
            for line in lines:
                selected=by_line[line.line_id]
                output.append(await self._reserve_row(s,account,order,line,selected['card_id'],mode=mode,
                    operation_key=line.payment_key,allow_closed=allow_closed,
                    rule_id=selected.get('rule_id'),rule_version=selected.get('rule_version'),
                    line_ids=[entry.line_id for entry in lines],confirmation_key=lines[0].payment_key))
            return output

    async def _reserve_row(self,s,account,order,line,card_id,*,mode,operation_key,allow_closed=False,allow_resend=False,resend_reason=None,rule_id=None,rule_version=None,quantity_override=None,line_ids,confirmation_key):
        if mode not in {'send_first','confirm_first','card_only'}: raise ValueError('发货模式无效')
        owner,account_id,order_no=account.owner_id,account.account_id,order.order_no
        version=snapshot(account)
        previous=(await s.execute(select(DeliveryIntent).where(DeliveryIntent.owner_id==owner,DeliveryIntent.account_id==account_id,DeliveryIntent.order_no==order_no,DeliveryIntent.operation_key==operation_key))).scalar_one_or_none()
        if previous: return previous
        if not allow_resend and order.status not in {'pending_ship','paid','pending','processing'} and not (allow_closed and mode=='card_only' and order.status in {'closed','cancelled'}):
            raise ValueError('订单状态待核实或不适合发货')
        if allow_resend and order.status in {'refunding','refunded','closed','cancelled'}: raise ValueError('退款或关闭订单需先核实')
        if (order.metadata_json or {}).get('status_conflict'): raise ValueError('订单状态待核实')
        if not allow_resend and (order.card_only_delivered or order.delivery_content): raise ValueError('历史履约需先核实')
        card=(await s.execute(select(Card).where(Card.id==card_id,Card.user_id==owner).with_for_update())).scalar_one_or_none()
        if not card or not card.enabled: raise ValueError('卡券不存在或已暂停')
        if card.type not in {'text','data','api','image'}: raise ValueError('卡券类型待集成')
        if not sku_matches(card,line):
            raise ValueError('订单规格缺失或未匹配')
        if type(line.quantity) is not int or not 1<=line.quantity<=1000: raise ValueError('订单数量无效')
        multiplier=1; rule=None
        if rule_id:
            rule=await s.scalar(select(DeliveryRule).where(DeliveryRule.id==rule_id,DeliveryRule.owner_id==owner).with_for_update())
            if not rule or not rule.enabled or rule.version!=rule_version or rule.card_id!=card_id or rule.account_id not in {None,account_id}:
                raise ValueError('发货规则已变化，请重新预览')
            multiplier=rule.delivery_count
            title=line.title
            if not title:
                from common.models.xy_catalog_item import XYCatalogItem
                title=await s.scalar(select(XYCatalogItem.title).where(XYCatalogItem.owner_id==owner,
                    XYCatalogItem.account_pk==account.id,XYCatalogItem.item_id==line.item_id).limit(1)) or ''
            if title_score(rule,title) is None: raise ValueError('订单标题与发货规则已不匹配')
        quantity=await delivery_quantity(s,account,card,line,rule)
        if quantity_override is not None: quantity=quantity_override
        if not 1<=quantity<=1000: raise ValueError('发货总数超过上限')
        if allow_closed: quantity=1
        lines=[]; texts=[]
        context={k:str(getattr(order,k,None) or '') for k in ('buyer_id','chat_id')}
        context.update({k:str(getattr(line,k) or '') for k in ('spec_name','spec_value','item_id')})
        context.update(order_id=order_no,line_id=line.line_id,order_quantity=str(line.quantity),order_amount=line.amount or '')
        if card.type=='data':
            stock=[line.strip() for line in (card.data_content or '').splitlines() if line.strip()]
            if len(stock)<quantity: raise ValueError('库存不足，未出库')
            lines=stock[:quantity]; card.data_content='\n'.join(stock[quantity:]); texts=list(lines)
        elif card.type=='text': texts=[card.text_content]*quantity if card.text_content else []
        if texts: texts=[process_delivery_content_with_description(t,card.description or '',context) for t in texts]
        images=json.loads(card.image_urls) if card.image_urls else ([card.image_url] if card.image_url else [])
        if card.type=='image': images=images*quantity
        if not isinstance(images,list) or not all(isinstance(u,str) and u for u in images): raise ValueError('图片配置无效')
        if card.type!='api' and not texts and not images: raise ValueError('卡券内容为空')
        row=DeliveryIntent(owner_id=owner,account_id=account_id,order_no=order_no,operation_key=operation_key,line_id=line.line_id,
             card_id=card_id,card_type=card.type,quantity=quantity,mode=mode,content_state='reserved',
             confirm_state='not_required' if mode=='card_only' or confirmation_key!=operation_key else 'pending',reserved_lines=lines,
             payload=None if card.type=='api' else {'texts':texts,'images':images},
             evidence=[{'actor_id':owner,'reason':resend_reason,'phase':'resend_requested'}] if allow_resend else [],
             source_snapshot={'line_signature':line.signature,'line_ids':line_ids,'confirmation_key':confirmation_key,'line_amount':line.amount,'rule_id':rule_id,'rule_version':rule_version,'multiplier':multiplier,'card_name':card.name,'card_updated_at':str(card.updated_at or ''),'spec_name':card.spec_name,'spec_value':card.spec_value,'order_quantity':line.quantity,'card_source':'own','api_config':card.api_config,'description':card.description or '', 'context':context,'images':images,'allow_closed':allow_closed},
             generation=version['generation'],credential_version=version['credential_version'],config_version=version['config_version'],
             not_before=int(time.time())+max(0,card.delay_seconds or 0) if card.delay_seconds else 0)
        if not self._current(account,row): raise ValueError('账号业务已暂停')
        s.add(row); await s.flush(); return row


    async def _claim(self,intent_id,owner,phase,now):
        async with self.sessions() as s,s.begin():
            row=await self._intent(s,intent_id,owner)
            account=await self._account(s,owner,row.account_id)
            if not self._current(account,row) or now<row.not_before: return None
            order=await s.scalar(select(XYOrder).where(XYOrder.owner_id==owner,XYOrder.account_id==row.account_id,XYOrder.order_no==row.order_no).with_for_update())
            if not order or (order.metadata_json or {}).get('status_conflict'): return None
            source=row.source_snapshot or {}
            context=source.get('context',{})
            if any(context.get(key) and context[key]!=str(getattr(order,key) or '') for key in ('buyer_id','chat_id')): return None
            try: current_line=next(line for line in order_lines(order) if line.line_id==row.line_id)
            except (ValueError,StopIteration): return None
            if source.get('line_signature') and source['line_signature']!=current_line.signature: return None
            if context.get('item_id') and context['item_id']!=(current_line.item_id or ''): return None
            group=await self._group(s,row)
            leader=next((entry for entry in group if entry.operation_key==source.get('confirmation_key',row.operation_key)),row)
            confirmation=leader.confirm_state
            allowed = order.status in {'pending_ship','paid','pending','processing'}
            allowed = allowed or (order.status in {'shipped','completed'} and (confirmation=='confirmed' or row.operation_key.startswith('resend:')))
            allowed = allowed or (order.status in {'closed','cancelled'} and (row.source_snapshot or {}).get('allow_closed') and row.mode=='card_only')
            if not allowed: return None
            if phase in {'purchase','content'} and (row.source_snapshot or {}).get('rule_id'):
                rule=await s.get(DeliveryRule,row.source_snapshot['rule_id'])
                if not rule or not rule.enabled or rule.version!=row.source_snapshot['rule_version']: return None
            if phase=='purchase':
                if row.card_type!='api' or row.content_state!='reserved' or row.payload is not None: return None
                if row.mode=='confirm_first' and confirmation!='confirmed': return None
                row.content_state='purchasing'
            elif phase=='content':
                if row.content_state!='reserved' or not row.payload: return None
                if row.mode=='confirm_first' and confirmation!='confirmed': return None
                row.content_state='sending'
            else:
                if row.confirm_state!='pending' or row.confirm_attempts>=1+len(CONFIRM_RETRY_DELAYS) or now<row.next_retry_at: return None
                if row.mode!='confirm_first' and (any(entry.content_state!='confirmed' for entry in group) or len(group)!=len(source.get('line_ids',[row.line_id]))): return None
                row.confirm_state='confirming'; row.confirm_attempts+=1
            await s.flush(); return row

    async def _finish(self,intent_id,owner,phase,result,now,payload=None):
        async with self.sessions() as s,s.begin():
            row=await self._intent(s,intent_id,owner)
            account=await self._account(s,owner,row.account_id)
            if not self._current(account,row): return
            expected={'purchase':'purchasing','content':'sending','confirm':'confirming'}[phase]
            field='confirm_state' if phase=='confirm' else 'content_state'
            if getattr(row,field)!=expected: return
            if phase=='purchase':
                row.content_state='reserved' if payload else 'unknown'; row.payload=payload
            elif phase=='content':
                row.content_state=result
                if result=='not_sent': await self._release(s,row)
                elif result=='confirmed': await self._count_confirmed(s,row)
            else:
                row.confirm_state='confirmed' if result=='confirmed' else ('pending' if result=='not_sent' else 'unknown')
                row.next_retry_at=(int(now+CONFIRM_RETRY_DELAYS[row.confirm_attempts-1])
                    if result=='not_sent' and row.confirm_attempts<=len(CONFIRM_RETRY_DELAYS) else 0)
            row.last_error=None if result=='confirmed' else ('result_unknown' if result=='unknown' else 'not_sent')
            if phase=='confirm' and result=='not_sent' and row.confirm_attempts>len(CONFIRM_RETRY_DELAYS):
                row.last_error='confirmation_retry_exhausted'
            await self._project(s,row)

    async def _count_confirmed(self,s,row):
        card=await s.scalar(select(Card).where(Card.id==row.card_id,Card.user_id==row.owner_id).with_for_update())
        if card: card.delivery_count=(card.delivery_count or 0)+row.quantity
        rule_id=(row.source_snapshot or {}).get('rule_id')
        if rule_id:
            rule=await s.scalar(select(DeliveryRule).where(DeliveryRule.id==rule_id,DeliveryRule.owner_id==row.owner_id).with_for_update())
            if rule: rule.delivery_times+=1

    async def _release(self,s,row):
        if row.reserved_lines:
            card=(await s.execute(select(Card).where(Card.id==row.card_id,Card.user_id==row.owner_id).with_for_update())).scalar_one()
            card.data_content='\n'.join(row.reserved_lines+([card.data_content] if card.data_content else []))
            row.reserved_lines=[]

    async def _group(self,s,row):
        key=(row.source_snapshot or {}).get('confirmation_key')
        if not key or row.operation_key.startswith('resend:'): return [row]
        entries=(await s.scalars(select(DeliveryIntent).where(DeliveryIntent.owner_id==row.owner_id,
            DeliveryIntent.account_id==row.account_id,DeliveryIntent.order_no==row.order_no))).all()
        return [entry for entry in entries if (entry.source_snapshot or {}).get('confirmation_key')==key]

    async def _project(self,s,row):
        order=(await s.execute(select(XYOrder).where(XYOrder.owner_id==row.owner_id,XYOrder.account_id==row.account_id,XYOrder.order_no==row.order_no).with_for_update())).scalar_one_or_none()
        if not order: return
        group=await self._group(s,row)
        source=row.source_snapshot or {}
        leader=next((entry for entry in group if entry.operation_key==source.get('confirmation_key',row.operation_key)),row)
        complete=len(group)==len(source.get('line_ids',[row.line_id])) and all(entry.content_state=='confirmed' for entry in group)
        states={entry.content_state for entry in group}
        if len(group)!=len(source.get('line_ids',[row.line_id])) or states & {'unknown','sending','purchasing'}:
            content_state='unknown'
        else:
            content_state=next(iter(states)) if len(states)==1 else 'partial'
        meta=dict(order.metadata_json or {})
        meta['fulfillment']={'intent_id':leader.id,'content_state':content_state,
            'confirm_state':leader.confirm_state,'lines':[{'line_id':entry.line_id,'intent_id':entry.id,
            'content_state':entry.content_state} for entry in group]}
        order.metadata_json=meta
        if complete:
            order.delivery_method='auto'
            order.delivery_content='\n'.join(value for entry in group for value in
                (entry.payload or {}).get('texts',[])+(entry.payload or {}).get('images',[]))[:2000]
            if row.mode=='card_only': order.card_only_delivered=True
        if leader.confirm_state=='confirmed' and order.status in {'pending_ship','paid','pending','processing'}: order.status='shipped'

    async def execute(self,intent_id,owner,*,send,confirm,purchase=None,now=None,check=None):
        now=time.time() if now is None else now
        async with self.sessions() as s:
            row=await self._intent(s,intent_id,owner)
            mode=row.mode
        # 接任者仅认领明确未在途的阶段；旧实例仍需通过check及版本条件回写。
        if check is not None and await self._allowed(check):
            async with self.sessions() as s,s.begin():
                row=await self._intent(s,intent_id,owner)
                account=await self._account(s,owner,row.account_id); current=snapshot(account)
                if (row.content_state in {'reserved','confirmed'} and row.confirm_state in {'pending','confirmed','not_required'}
                    and row.generation < current['generation']
                    and row.credential_version==current['credential_version'] and row.config_version==current['config_version']):
                    row.generation=current['generation']
        # 同一入口用于重复付款、延迟任务及仅补确认。崩溃的 sending/confirming 不重试。
        if mode=='confirm_first': await self._external(intent_id,owner,'confirm',confirm,now,check)
        if purchase and await self._allowed(check):
            claimed=await self._claim(intent_id,owner,'purchase',now)
            if claimed:
                payload=None
                try:
                    source=claimed.source_snapshot or {}
                    payload=await purchase(source.get('api_config'),quantity=claimed.quantity,idempotency_key=claimed.id,context=source.get('context',{}))
                    if (not isinstance(payload,dict) or not isinstance(payload.get('texts'),list) or
                        len(payload['texts'])!=claimed.quantity or not all(isinstance(t,str) and t.strip() for t in payload['texts'])):
                        payload=None
                    else:
                        payload={'texts':[process_delivery_content_with_description(t,source.get('description',''),source.get('context',{})) for t in payload['texts']],
                                 'images':source.get('images',[])}
                except Exception: pass
                if await self._allowed(check):
                    await self._finish(intent_id,owner,'purchase','confirmed' if payload else 'unknown',now,payload)
        await self._external(intent_id,owner,'content',send,now,check)
        if mode!='confirm_first': await self._external(intent_id,owner,'confirm',confirm,now,check)
        async with self.sessions() as s: return await self._intent(s,intent_id,owner)

    @staticmethod
    async def _allowed(check):
        if check is None: return True
        try:
            await check()
            return True
        except Exception: return False

    async def _external(self,intent_id,owner,phase,callback,now,check):
        if callback is None or not await self._allowed(check): return
        claimed=await self._claim(intent_id,owner,phase,now)
        if not claimed: return
        try:
            result=await callback(claimed.payload) if phase=='content' else await callback(claimed.order_no)
            if result not in {'confirmed','not_sent','unknown'}: result='unknown'
        except Exception: result='unknown'
        if await self._allowed(check):
            await self._finish(intent_id,owner,phase,result,now)

    async def reconcile(self,intent_id,owner,phase,result,*,evidence):
        if phase not in {'content','confirm'} or result not in {'confirmed','not_sent'} or not evidence or len(evidence)>120:
            raise ValueError('需要明确结果和核实凭据编号')
        async with self.sessions() as s,s.begin():
            row=await self._intent(s,intent_id,owner)
            field='content_state' if phase=='content' else 'confirm_state'
            if getattr(row,field) not in {'unknown','sending','confirming'}: raise ValueError('此阶段没有待核实结果')
            if phase=='content' and row.payload is None: raise ValueError('供应商结果需先核实内容')
            setattr(row,field,'pending' if phase=='confirm' and result=='not_sent' else result)
            if phase=='confirm' and result=='not_sent':
                latest={}
                for event in row.evidence or []:
                    if event.get('phase','').startswith('confirm:'): latest[event['phase']]=event['result']
                row.evidence=(row.evidence or [])+[{'phase':step,'result':'not_sent','reference':evidence}
                    for step,state in latest.items() if state in {'sending','unknown'}]
            if phase=='content' and result=='not_sent': await self._release(s,row)
            elif phase=='content' and result=='confirmed': await self._count_confirmed(s,row)
            row.evidence=(row.evidence or [])+[{'phase':phase,'result':result,'reference':evidence}]
            await self._project(s,row)
            return row

    async def resend(self,intent_id,owner,*,request_id,acknowledged,reason=None):
        import re
        if not reason or not reason.strip() or len(reason)>200: raise ValueError('请填写补发原因')
        if not acknowledged or not re.fullmatch(r'[A-Za-z0-9_-]{8,40}',request_id):
            raise ValueError('请确认再次出库和发送的影响')
        async with self.sessions() as s:
            old=await self._intent(s,intent_id,owner)
            if old.content_state!='confirmed': raise ValueError('旧履约内容尚未确认，先核实')
        return await self.reserve(owner,old.account_id,old.order_no,old.card_id,mode='card_only',
                                  operation_key='resend:'+request_id,allow_resend=True,resend_reason=reason.strip(),line_id=old.line_id,quantity_override=old.quantity)

    async def verify_supplier(self,intent_id,owner,*,query,check=None):
        if not await self._allowed(check): raise ValueError('账号执行权已失效')
        async with self.sessions() as s:
            row=await self._intent(s,intent_id,owner)
            if row.card_type!='api' or row.content_state not in {'unknown','purchasing'} or row.payload is not None:
                raise ValueError('此履约没有待核实采购')
            source=row.source_snapshot or {}
            versions=(row.generation,row.credential_version,row.config_version)
        payload=await query(source.get('api_config'),quantity=row.quantity,idempotency_key=row.id,context=source.get('context',{}))
        if not isinstance(payload,dict) or not isinstance(payload.get('texts'),list) or len(payload['texts'])!=row.quantity or not all(isinstance(t,str) and t.strip() for t in payload['texts']):
            raise ValueError('供应商未给出完整内容证据')
        if not await self._allowed(check): raise ValueError('账号执行权已失效')
        async with self.sessions() as s,s.begin():
            row=await self._intent(s,intent_id,owner)
            if row.content_state not in {'unknown','purchasing'} or row.payload is not None: return row
            account=await self._account(s,owner,row.account_id)
            if versions!=(row.generation,row.credential_version,row.config_version) or not self._current(account,row): raise ValueError('采购核实期间账号版本变化')
            row.payload={'texts':[process_delivery_content_with_description(t,source.get('description',''),source.get('context',{})) for t in payload['texts']], 'images':source.get('images',[])}
            row.content_state='reserved';row.last_error=None
            row.evidence=(row.evidence or [])+[{'phase':'purchase','result':'confirmed','reference':row.id}]
            return row

    async def confirm_step(self,intent_id,owner,step,callback,*,check=None):
        if not await self._allowed(check): return 'unknown'
        async with self.sessions() as s,s.begin():
            row=await self._intent(s,intent_id,owner)
            account=await self._account(s,owner,row.account_id)
            if not self._current(account,row): return 'unknown'
            events=[e for e in (row.evidence or []) if e.get('phase')=='confirm:'+step]
            if events and events[-1]['result'] in {'confirmed','sending','unknown'}:
                return 'confirmed' if events[-1]['result']=='confirmed' else 'unknown'
            row.evidence=(row.evidence or [])+[{'phase':'confirm:'+step,'result':'sending'}]
        try:
            result=await callback()
            outcome='confirmed' if isinstance(result,dict) and result.get('success') else (result or {}).get('outcome','unknown')
            if outcome not in {'confirmed','not_sent'}: outcome='unknown'
        except Exception: outcome='unknown'
        if not await self._allowed(check): return 'unknown'
        async with self.sessions() as s,s.begin():
            row=await self._intent(s,intent_id,owner)
            account=await self._account(s,owner,row.account_id)
            if not self._current(account,row): return 'unknown'
            row.evidence=(row.evidence or [])+[{'phase':'confirm:'+step,'result':outcome}]
        return outcome

    async def record_content(self,intent_id,owner,part,state):
        if state not in {'sending','confirmed','not_sent','unknown'}: raise ValueError('内容回执状态无效')
        async with self.sessions() as s,s.begin():
            row=await self._intent(s,intent_id,owner)
            account=await self._account(s,owner,row.account_id)
            if not self._current(account,row) or row.content_state!='sending': raise ValueError('发送执行权已失效')
            row.evidence=(row.evidence or [])+[{'phase':'content_piece','part':part,'result':state}]

    async def reconcile_purchase(self,intent_id,owner,*,texts,evidence):
        import re
        if not evidence or not re.fullmatch(r'[\w.:/-]{1,120}',evidence): raise ValueError('需要采购凭据编号')
        async with self.sessions() as s,s.begin():
            row=await self._intent(s,intent_id,owner)
            if row.card_type!='api' or row.content_state not in {'unknown','purchasing'} or row.payload is not None:
                raise ValueError('此履约没有待核实采购')
            if not isinstance(texts,list) or len(texts)!=row.quantity or not all(isinstance(t,str) and t.strip() for t in texts):
                raise ValueError('采购证据数量与订单不一致')
            source=row.source_snapshot or {}
            row.payload={'texts':[process_delivery_content_with_description(t,source.get('description',''),source.get('context',{})) for t in texts], 'images':source.get('images',[])}
            row.content_state='reserved';row.last_error=None
            row.evidence=(row.evidence or [])+[{'phase':'purchase','result':'confirmed','reference':evidence,'actor_id':owner,'source':'manual'}]
            return row
