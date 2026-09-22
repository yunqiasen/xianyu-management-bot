import time
from hashlib import sha256
from sqlalchemy import select,update
from common.models.xy_account import XYAccount
from common.models.notification_channel import NotificationChannel
from common.models.message_notification import MessageNotification
from common.models.notification_delivery import NotificationEvent,NotificationDelivery
from common.services.notification_template_service import NotificationTemplateService, VARIABLES, RECOVERY_EVENTS, template_values
from common.services.notification_transport import deliver
from common.utils.logging_utils import redact_secrets

class NotificationDeliveryService:
    RETRIES=(60,300,900)
    def __init__(self,session,clock=time.time,sender=deliver):
        self.session,self.clock,self.sender=session,clock,sender
    async def publish(self,owner_id,account_id,event_type,generation,payload,*,commit=True):
        account=(await self.session.execute(select(XYAccount).where(XYAccount.owner_id==owner_id,XYAccount.account_id==account_id).with_for_update())).scalar_one_or_none()
        if not account: raise ValueError('账号归属不匹配')
        now=self.clock()
        # 故障告警滑动窗口以首次事件为起点；恢复身份永久唯一。
        identity=sha256(f'{owner_id}:{account.id}:{event_type}:{generation}'.encode()).hexdigest()
        event=(await self.session.execute(select(NotificationEvent).where(NotificationEvent.owner_id==owner_id,NotificationEvent.account_id==account_id,NotificationEvent.event_type==event_type,NotificationEvent.generation==generation).order_by(NotificationEvent.last_seen.desc()).limit(1))).scalar_one_or_none()
        if event and (event_type in RECOVERY_EVENTS or now-event.first_seen<900):
            event.merged_count+=1; event.last_seen=now
            await (self.session.commit() if commit else self.session.flush()); return event
        if event:
            identity=sha256(f'{identity}:{now}'.encode()).hexdigest()
        clean=template_values(event_type, payload)
        clean.update(account_id=account_id,event_type=event_type,generation=generation)
        event=NotificationEvent(id=identity,owner_id=owner_id,account_id=account_id,event_type=event_type,generation=generation,payload=clean,first_seen=now,last_seen=now)
        self.session.add(event)
        channels=(await self.session.execute(select(NotificationChannel).join(MessageNotification,MessageNotification.channel_id==NotificationChannel.id).where(MessageNotification.owner_id==owner_id,MessageNotification.account_pk==account.id,MessageNotification.enabled.is_(True),NotificationChannel.owner_id==owner_id,NotificationChannel.enabled.is_(True)))).scalars().unique().all()
        rendered,failed=await NotificationTemplateService(self.session).render_event(owner_id,event_type,clean)
        for channel in channels:
            self.session.add(NotificationDelivery(event_id=identity,owner_id=owner_id,channel_id=channel.id,rendered=rendered,template_failed=failed,due_at=now))
        await (self.session.commit() if commit else self.session.flush())
        return event
    async def observe_account_states(self):
        """Persist the observed outage generation in the same transaction as its event."""
        from common.services.account_policy import snapshot
        ids = (await self.session.scalars(select(XYAccount.id).order_by(XYAccount.id))).all()
        await self.session.commit()
        for account_pk in ids:
            account = await self.session.scalar(select(XYAccount).where(XYAccount.id == account_pk).with_for_update())
            if account is None or account.status != 'active':
                await self.session.commit(); continue
            current = snapshot(account)
            state, reason = current['business_state'], current.get('reason') or ''
            metadata = dict(account.metadata_json or {})
            previous = dict(metadata.get('notification_observer') or {})
            if (state, reason) == (previous.get('state'), previous.get('reason')):
                await self.session.commit(); continue
            observed = {**previous, 'state': state, 'reason': reason}
            kind = None
            if state in {'paused', 'proxy_error', 'verification_required', 'cooldown'}:
                if not previous.get('outage'):
                    observed['fault'] = int(previous.get('fault', 0)) + 1
                observed['outage'] = True
                kind = {'paused':'account_paused', 'verification_required':'face_verify'}.get(state, 'account_error')
            elif state == 'ready' and previous.get('outage'):
                observed['outage'] = False
                kind = 'recovered'
            if kind:
                await self.publish(account.owner_id, account.account_id, kind, 'fault:'+str(observed['fault']),
                    {'summary': '账号已恢复' if kind == 'recovered' else '请在后台查看账号状态',
                     'pause_reason': reason, 'status_note': state}, commit=False)
            metadata['notification_observer'] = observed
            account.metadata_json = metadata
            await self.session.commit()

    async def dispatch_due(self,owner_id=None,limit=100,event_id=None):
        now=self.clock()
        # 崩溃后的提交态只转待核实，不自动重发。
        stale=update(NotificationDelivery).where(NotificationDelivery.status=='sending',NotificationDelivery.due_at<now-60)
        if owner_id is not None: stale=stale.where(NotificationDelivery.owner_id==owner_id)
        await self.session.execute(stale.values(status='unknown'))
        stmt=select(NotificationDelivery.id).where(NotificationDelivery.status.in_(['pending','not_accepted']),NotificationDelivery.due_at<=now).limit(limit)
        if owner_id is not None: stmt=stmt.where(NotificationDelivery.owner_id==owner_id)
        if event_id is not None: stmt=stmt.where(NotificationDelivery.event_id==event_id)
        ids=(await self.session.execute(stmt)).scalars().all(); await self.session.commit()
        for id_ in ids:
            claimed=await self.session.execute(update(NotificationDelivery).where(NotificationDelivery.id==id_,NotificationDelivery.status.in_(['pending','not_accepted']),NotificationDelivery.due_at<=now).values(status='sending',due_at=now,attempts=NotificationDelivery.attempts+1))
            await self.session.commit()
            if not claimed.rowcount: continue
            item=await self.session.get(NotificationDelivery,id_,populate_existing=True)
            channel=(await self.session.execute(select(NotificationChannel).where(NotificationChannel.id==item.channel_id,NotificationChannel.owner_id==item.owner_id,NotificationChannel.enabled.is_(True)))).scalar_one_or_none()
            if not channel: outcome='cancelled'
            else:
                try: outcome=await self.sender(channel.channel_type,channel.config_payload,item.rendered)
                except Exception: outcome='unknown'
            if outcome not in ('accepted','not_accepted','unknown','cancelled'): outcome='unknown'
            if outcome=='not_accepted':
                if item.attempts<=len(self.RETRIES): item.due_at=now+self.RETRIES[item.attempts-1]
                else: outcome='exhausted'
            item.status=outcome
            await self.session.commit()
        return len(ids)
