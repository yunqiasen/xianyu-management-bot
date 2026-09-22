from support import *
from app.services.notification_service import NotificationChannelService
from common.schemas.notification import NotificationChannelCreate
import httpx
from unittest.mock import patch
class NotificationTests(DatabaseCase):
    async def test_invalid_qq_config_rejected_on_create(self):
        service=NotificationChannelService(self.session)
        with self.assertRaises(ValueError):
            await service.create_channel(1,NotificationChannelCreate(name='QQ',type='qq',config={}))
    async def test_delivery_service_contract(self):
        import common.services as services
        from pathlib import Path
        self.assertTrue((ROOT/'common/services/notification_delivery_service.py').exists(), '缺持久投递业务入口')
    async def test_event_dedupe_retry_unknown_and_owner_isolation(self):
        try:
            from common.services.notification_delivery_service import NotificationDeliveryService
            from common.models.notification_delivery import NotificationEvent, NotificationDelivery
        except ImportError:
            self.fail('通知事件/投递尚未分离保存')
        self.session.add_all([NotificationChannel(id=10,owner_id=1,name='web',channel_type='webhook',config_payload={'webhook_url':'https://example.test'},enabled=True),NotificationChannel(id=20,owner_id=2,name='other',channel_type='webhook',config_payload={},enabled=True),MessageNotification(id=1,owner_id=1,account_pk=1,account_identifier='account-a',channel_id=10),MessageNotification(id=2,owner_id=1,account_pk=1,account_identifier='account-a',channel_id=20)])
        await self.session.commit()
        now=[1000.0]; outcomes=['not_accepted','unknown']; calls=[]
        async def send(kind,config,text):
            calls.append(text); return outcomes.pop(0)
        service=NotificationDeliveryService(self.session,clock=lambda:now[0],sender=send)
        first=await service.publish(1,'account-a','account_error','fault-1',{'summary':'token=SAMPLE_SECRET'})
        repeat=await service.publish(1,'account-a','account_error','fault-1',{'summary':'repeated'})
        self.assertEqual(first.id,repeat.id); self.assertEqual(repeat.merged_count,2)
        from sqlalchemy import select
        deliveries=(await self.session.execute(select(NotificationDelivery))).scalars().all()
        self.assertEqual(len(deliveries),1)
        self.assertNotIn('SAMPLE_SECRET',str(first.payload))
        await service.dispatch_due(); self.assertEqual(len(calls),1)
        now[0]+=59; await service.dispatch_due(); self.assertEqual(len(calls),1)
        now[0]+=1; await service.dispatch_due(); self.assertEqual(len(calls),2)
        now[0]+=10000; await service.dispatch_due(); self.assertEqual(len(calls),2)
        self.assertEqual(deliveries[0].status,'unknown')
        a=await service.publish(1,'account-a','recovered','fault-1',{})
        now[0]+=9000; b=await service.publish(1,'account-a','recovered','fault-1',{})
        self.assertEqual(a.id,b.id)
        now[0]+=1000
        c=await service.publish(1,'account-a','account_error','fault-1',{})
        d=await service.publish(1,'account-a','account_error','fault-1',{})
        self.assertEqual(c.id,d.id)
        with self.assertRaises(ValueError):
            await service.publish(1,'account-b','account_error','f',{})
    async def test_qq_acceptance_and_timeout_are_different(self):
        try:
            from common.services.notification_transport import deliver
        except ImportError:
            self.fail('缺渠道受理结果适配')
        async def handler(request):
            self.assertEqual(request.url.path,'/send_private_msg')
            return httpx.Response(200,json={'status':'failed','retcode':100})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            outcome=await deliver('qq',{'base_url':'http://qq.test','user_id':'123'},'hello',client=client)
            self.assertEqual(outcome,'not_accepted')
        async def timeout(request): raise httpx.ReadTimeout('receipt lost')
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            self.assertEqual(await deliver('webhook',{'webhook_url':'http://fixture.test'},'hello',client=client),'unknown')
    async def test_channel_secret_read_masked_and_edit_preserves(self):
        svc=NotificationChannelService(self.session)
        result=await svc.create_channel(1,NotificationChannelCreate(name='qq',type='qq',config={'base_url':'http://qq.test','user_id':'1','access_token':'SAMPLE_SECRET'}))
        self.assertNotIn('SAMPLE_SECRET',str(result))
        from common.schemas.notification import NotificationChannelUpdate
        await svc.update_channel(1,result['id'],NotificationChannelUpdate(config=result['config']))
        row=await svc.get_channel(1,result['id']);self.assertEqual(row.config_payload['access_token'],'SAMPLE_SECRET')
    async def test_known_rejections_stop_after_three_retries(self):
        from common.services.notification_delivery_service import NotificationDeliveryService
        from common.models.notification_delivery import NotificationDelivery
        now=[100.0];calls=[]
        async def send(*args):calls.append(1);return 'not_accepted'
        self.session.add_all([NotificationChannel(id=1,owner_id=1,name='x',channel_type='webhook',config_payload={'webhook_url':'http://fixture.test'}),MessageNotification(id=1,owner_id=1,account_pk=1,account_identifier='account-a',channel_id=1)]);await self.session.commit()
        svc=NotificationDeliveryService(self.session,clock=lambda:now[0],sender=send)
        await svc.publish(1,'account-a','account_error','generation',{})
        for step in (0,60,300,900,10000):now[0]+=step;await svc.dispatch_due()
        self.assertEqual(len(calls),4)
        from sqlalchemy import select
        row=(await self.session.execute(select(NotificationDelivery))).scalar_one();self.assertEqual(row.status,'exhausted')
    async def test_channel_test_api_persists_business_rejection(self):
        from app.api.routes.notifications import channels_router
        from app.api import deps
        from fastapi import FastAPI
        from types import SimpleNamespace
        from unittest.mock import patch
        from sqlalchemy import select
        from common.models.notification_delivery import NotificationEvent,NotificationDelivery
        svc=NotificationChannelService(self.session)
        channel=await svc.create_channel(1,NotificationChannelCreate(name='ding',type='dingtalk',config={'webhook_url':'https://ding.test/hook'}))
        app=FastAPI();app.include_router(channels_router)
        app.dependency_overrides[deps.get_current_active_user]=lambda:SimpleNamespace(id=1)
        app.dependency_overrides[deps.get_notification_channel_service]=lambda:svc
        original=httpx.AsyncClient
        async def remote(request):return httpx.Response(200,json={'errcode':310000,'errmsg':'rejected'})
        async with original(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as c:
            with patch('common.services.notification_transport.httpx.AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(remote),**kw)):
                r=await c.post(f"/notification-channels/{channel['id']}/test")
        self.assertFalse(r.json()['success']);self.assertEqual(r.json()['data']['status'],'not_accepted')
        events=(await self.session.execute(select(NotificationEvent))).scalars().all();deliveries=(await self.session.execute(select(NotificationDelivery))).scalars().all()
        self.assertEqual(len(events),1);self.assertEqual(deliveries[0].attempts,1)
    async def test_alert_window_is_fifteen_minutes_from_first_event(self):
        from common.services.notification_delivery_service import NotificationDeliveryService
        now=[1000.0];svc=NotificationDeliveryService(self.session,clock=lambda:now[0])
        first=await svc.publish(1,'account-a','account_error','fault',{})
        now[0]+=899
        same=await svc.publish(1,'account-a','account_error','fault',{})
        self.assertEqual(first.id,same.id)
        now[0]+=2
        fresh=await svc.publish(1,'account-a','account_error','fault',{})
        self.assertNotEqual(first.id,fresh.id)
    async def test_scheduler_observes_fault_recovery_and_new_fault_after_restart(self):
        from common.services.notification_delivery_service import NotificationDeliveryService
        from common.models.xy_account import XYAccount
        from common.services.account_policy import snapshot, store
        from common.models.notification_delivery import NotificationEvent
        from sqlalchemy import select
        account = await self.session.get(XYAccount, 1)
        account.status = 'active'
        state = snapshot(account); state['business_state'] = 'ready'; store(account, state)
        await self.session.commit()
        service = NotificationDeliveryService(self.session)
        await service.observe_account_states()
        for business_state in ('paused', 'paused', 'ready', 'ready', 'paused'):
            account = await self.session.get(XYAccount, 1, populate_existing=True)
            state = snapshot(account); state['business_state'] = business_state
            state['reason'] = 'repeated_invalid_credentials' if business_state == 'paused' else None
            store(account, state); await self.session.commit()
            await NotificationDeliveryService(self.session).observe_account_states()
        events = (await self.session.scalars(select(NotificationEvent).order_by(NotificationEvent.created_at, NotificationEvent.generation))).all()
        self.assertEqual(sorted(e.event_type for e in events), ['account_paused', 'account_paused', 'recovered'])
        faults = [e.generation for e in events if e.event_type == 'account_paused']
        self.assertEqual(len(set(faults)), 2)
        self.assertIn(next(e.generation for e in events if e.event_type == 'recovered'), faults)
