"""Business notifications use one persistent, owner-scoped event/transport chain."""
import hashlib
import time
from loguru import logger
from sqlalchemy import select
from common.models.xy_account import XYAccount
from common.models.notification_delivery import NotificationDelivery
from common.services.notification_delivery_service import NotificationDeliveryService
from common.services.notification_template_service import DEFAULTS
from common.services.token_api_mode import TOKEN_API_NAMES
from common.utils.logging_utils import redact_secrets


class NotificationManager:
    def __init__(self, cookie_id, *, sessions=None, sender=None):
        if sessions is None:
            from common.db.session import async_session_maker
            sessions = async_session_maker
        self.cookie_id, self.sessions, self.sender = cookie_id, sessions, sender

    async def _filtered(self, content, source, item_id):
        from common.services.reply_state import ReplyState
        from common.services.reply_policy import evaluate_filters
        try:
            rules = await ReplyState(self.sessions).filters(self.cookie_id)
            return 'skip_notify' in evaluate_filters(rules, content, source, item_id or '').actions
        except Exception as exc:
            logger.warning('通知过滤配置读取失败: {}', type(exc).__name__)
            return True

    async def send_notification(self, send_user_name, send_user_id, send_message, item_id=None, chat_id=None, event_id=None):
        if send_message in {'发来一条消息', '发来一条新消息'}: return False
        if await self._filtered(send_message, 'user', item_id): return False
        payload = dict(buyer_name=send_user_name, buyer_id=send_user_id, item_id=item_id or '',
                       chat_id=chat_id or '', message=send_message, summary=send_message)
        identity = event_id or hashlib.sha256(f'{chat_id}:{send_user_id}:{send_message}'.encode()).hexdigest()
        return await self._publish('message', identity, payload)

    async def send_delivery_failure_notification(self, send_user_name, send_user_id, item_id, error_message, chat_id=None):
        if await self._filtered(error_message or '', 'system', item_id): return False
        identity = hashlib.sha256(f'{chat_id}:{send_user_id}:{item_id}:{error_message}'.encode()).hexdigest()
        return await self._publish('delivery', identity, dict(buyer_name=send_user_name, buyer_id=send_user_id,
            item_id=item_id or '', chat_id=chat_id or '', result='请在后台查看履约详情', summary='请在后台查看履约详情'))

    async def send_token_refresh_notification(self, error_message, notification_type='token_refresh',
                                               chat_id=None, attachment_path=None, verification_url=None):
        if self._is_normal_token_expiry(error_message): return False
        aliases = {'captcha_success_auto_update': 'slider_success', 'face_verification_required': 'face_verify',
                   'password_login_verification': 'face_verify', 'baxia_punish_captcha': 'face_verify',
                   'account_disabled': 'account_paused'}
        event_type = aliases.get(notification_type, notification_type)
        if event_type not in DEFAULTS: event_type = 'token_refresh'
        async with self.sessions() as session:
            account = await session.scalar(select(XYAccount).where(XYAccount.account_id == self.cookie_id))
            if account is None: return False
            from common.services.account_policy import snapshot
            state = snapshot(account)
            generation = f"{state['generation']}:{state['credential_version']}:{notification_type}"
        payload = dict(summary='请在后台查看账号事件详情', error_message='请在后台查看账号事件详情',
                       status_text='验证结果已登记，请查看账号业务状态', chat_id=chat_id or '',
                       verification_url='/accounts', pause_reason=str(state.get('reason') or ''),
                       status_note=state['business_state'], cookie_count=len((account.cookie or '').split(';')))
        return await self._publish(event_type, generation, payload)

    async def _publish(self, event_type, generation, payload):
        try:
            async with self.sessions() as session:
                account = await session.scalar(select(XYAccount).where(XYAccount.account_id == self.cookie_id))
                if account is None: return False
                options = {'sender': self.sender} if self.sender else {}
                service = NotificationDeliveryService(session, **options)
                event = await service.publish(account.owner_id, self.cookie_id, event_type, str(generation),
                    {**payload, 'time': time.strftime('%Y-%m-%d %H:%M:%S')})
                await service.dispatch_due(account.owner_id, event_id=event.id)
                states = (await session.scalars(select(NotificationDelivery.status).where(NotificationDelivery.event_id == event.id))).all()
                return 'accepted' in states
        except Exception as exc:
            logger.error('通知投递记录失败: {}', type(exc).__name__)
            return False

    async def _send_to_channels(self, notifications, message, attachment_path=None, event_type='message', generation=None):
        summary = message if event_type in {'message', 'test'} else '请在后台查看事件详情'
        return await self._publish(event_type, generation or hashlib.sha256(message.encode()).hexdigest(), {'summary': summary})

    async def send_account_event(self, event_type, generation, summary):
        return await self._send_to_channels([], summary, event_type=event_type, generation=generation)

    def _is_normal_token_expiry(self, error_message: str) -> bool:
        """检查是否是正常的令牌过期"""
        no_notification_keywords = [
            'FAIL_SYS_TOKEN_EXOIRED::令牌过期',
            'FAIL_SYS_TOKEN_EXPIRED::令牌过期',
            'FAIL_SYS_TOKEN_EXOIRED',
            'FAIL_SYS_TOKEN_EXPIRED',
            'FAIL_SYS_TOKEN_EMPTY::令牌为空',
            'FAIL_SYS_TOKEN_EMPTY',
            '令牌过期',
            '令牌为空',
            'FAIL_SYS_SESSION_EXPIRED::Session过期',
            'FAIL_SYS_SESSION_EXPIRED',
            'Session过期',
            'Token定时刷新失败，将自动重试',
            'Token定时刷新失败'
        ]

        for keyword in no_notification_keywords:
            if keyword in error_message:
                return True
        return False

    def _is_token_related_error(self, error_message: str) -> bool:
        """检查是否是Token相关的错误"""
        token_error_keywords = [
            'Token刷新失败', 'Token刷新异常', 'token刷新失败', 'token刷新异常',
            'TOKEN刷新失败', 'TOKEN刷新异常',
            'FAIL_SYS_USER_VALIDATE', 'RGV587_ERROR',
            '哎哟喂,被挤爆啦', '请稍后重试',
            'punish?x5secdata', 'captcha',
            '无法获取有效token', '无法获取有效Token',
            'Token获取失败', 'token获取失败', 'TOKEN获取失败',
            'Token定时刷新失败', 'token定时刷新失败', 'TOKEN定时刷新失败',
            '初始化时无法获取有效Token', '初始化时无法获取有效token',
            'accessToken', 'access_token', '_m_h5_tk',
            # 识别内置网页 Token 接口名，避免接口报错漏判
            *TOKEN_API_NAMES.values(),
        ]

        error_message_lower = error_message.lower()
        for keyword in token_error_keywords:
            if keyword.lower() in error_message_lower:
                return True
        return False
