from support import *
from common.services.notification_template_service import NotificationTemplateService
class TemplateTests(DatabaseCase):
    async def test_preview_save_reset_is_owner_and_event_scoped(self):
        svc=NotificationTemplateService(self.session)
        await svc.save(1,'account_error','{account_id}: {summary}')
        await svc.save(1,'message','消息 {summary}')
        await svc.save(2,'account_error','其他 {summary}')
        text,failed=await svc.render_event(1,'account_error',{'account_id':'a','summary':'cookie=SAMPLE_VALUE'})
        self.assertFalse(failed); self.assertNotIn('SAMPLE_VALUE',text)
        text,failed=svc.render('account_error','{cookie}',{'cookie':'SAMPLE_VALUE'})
        self.assertTrue(failed); self.assertNotIn('SAMPLE_VALUE',text)
        await svc.reset(1,'account_error')
        self.assertIsNone(await svc.get(1,'account_error'))
        self.assertIsNotNone(await svc.get(1,'message')); self.assertIsNotNone(await svc.get(2,'account_error'))
    async def test_template_test_endpoint_uses_same_renderer(self):
        from fastapi import FastAPI
        from app.api.routes.notifications import channels_router
        from app.api import deps
        from types import SimpleNamespace
        import httpx
        app=FastAPI(); app.include_router(channels_router)
        app.dependency_overrides[deps.get_current_active_user]=lambda: SimpleNamespace(id=1)
        app.dependency_overrides[deps.get_db_session]=lambda:self.session
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as c:
            r=await c.post('/notification-channels/templates/account_error/preview',json={'body':'{account_id}: {summary}','variables':{'account_id':'a','summary':'hello'}})
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.json()['text'],'a: hello')
