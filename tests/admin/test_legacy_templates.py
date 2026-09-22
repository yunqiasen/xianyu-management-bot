"""Notification templates exercise business events, persistence and channel output."""
import importlib.util
from pathlib import Path
from sqlalchemy import select
from support import DatabaseCase, NotificationChannel, MessageNotification, NotificationEvent, NotificationDelivery
from common.services.notification_template_service import NotificationTemplateService
from common.services.notification_delivery_service import NotificationDeliveryService


class LegacyTemplateTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from common.models.reply_state import advanced_filters
        async with self.engine.begin() as conn:
            await conn.run_sync(advanced_filters.create)

    async def test_distinct_legacy_events_and_buyer_variables_are_rendered(self):
        templates = NotificationTemplateService(self.session)
        await templates.save(1, 'token_refresh', '续期: {error_message}')
        await templates.save(1, 'account_paused', '暂停: {pause_reason} / {action_hint}')
        await templates.save(1, 'message', '{buyer_name}/{buyer_id}/{item_id}/{chat_id}: {message}')
        text, failed = await templates.render_event(1, 'message', {
            'buyer_name': '张三', 'buyer_id': 'b1', 'item_id': 'i1', 'chat_id': 'c1', 'message': '还在吗'})
        self.assertFalse(failed)
        self.assertEqual(text, '张三/b1/i1/c1: 还在吗')
        paused, failed = await templates.render_event(1, 'account_paused', {'pause_reason': '两次失败', 'action_hint': '打开账号页'})
        self.assertEqual(paused, '暂停: 两次失败 / 打开账号页')
        self.assertFalse(failed)

    async def test_old_global_default_applies_to_new_owner_without_cross_user_override(self):
        templates = NotificationTemplateService(self.session)
        await templates.save(0, 'message', '旧全局 {message}')
        await templates.save(1, 'message', '自己的 {message}')
        self.assertEqual((await templates.render_event(2, 'message', {'message': 'hi'}))[0], '旧全局 hi')
        self.assertEqual((await templates.render_event(1, 'message', {'message': 'hi'}))[0], '自己的 hi')

    async def test_delivery_entry_keeps_content_out_of_notification_and_populates_identity(self):
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location('fixture_notification_manager', root / 'websocket/app/services/xianyu/notification_manager.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.session.add_all([
            NotificationChannel(id=10, owner_id=1, name='fixture', channel_type='webhook', config_payload={'webhook_url':'https://fixture.test'}),
            MessageNotification(id=1, owner_id=1, account_pk=1, account_identifier='account-a', channel_id=10)])
        await self.session.commit()
        await NotificationTemplateService(self.session).save(1, 'delivery', '{buyer_name}/{item_id}: {result}')
        output = []
        async def channel(kind, config, text):
            output.append(text); return 'accepted'
        from sqlalchemy.ext.asyncio import async_sessionmaker
        manager = module.NotificationManager('account-a', sessions=async_sessionmaker(self.engine, expire_on_commit=False), sender=channel)
        await manager.send_delivery_failure_notification('张三', 'buyer1', 'item1', '发货内容 ORDINARY_CARD_123', 'chat1')
        self.assertEqual(output, ['张三/item1: 请在后台查看履约详情'])
        events = (await self.session.scalars(select(NotificationEvent))).all()
        self.assertEqual(events[0].event_type, 'delivery')
        self.assertNotIn('ORDINARY_CARD_123', str(events[0].payload))
