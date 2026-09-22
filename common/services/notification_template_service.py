"""Controlled templates shared by previews, live notifications and legacy imports."""
from string import Formatter
from sqlalchemy import select
from common.models.notification_delivery import NotificationTemplate
from common.utils.logging_utils import redact_secrets

DEFAULTS = {
    'message': '{account_id} 收到消息：{summary}',
    'account_error': '{account_id} 账号异常：{summary}',
    'recovered': '{account_id} 已恢复',
    'delivery': '{account_id} 履约事件：{summary}',
    'test': '通知测试：{summary}',
    'token_refresh': '{account_id} 续期异常：{error_message}',
    'slider_success': '{account_id} 验证完成：{status_text}',
    'face_verify': '{account_id} 需要{verification_type}：{verification_action}\n{verification_url}',
    'password_login_success': '{account_id} 密码登录成功，Cookie字段数：{cookie_count}',
    'cookie_refresh_success': '{account_id} 凭据刷新成功，Cookie字段数：{cookie_count}',
    'account_paused': '{account_id} 已暂停：{pause_reason}\n{action_hint}',
}
VARIABLES = {
    'account_id': '未指定账号', 'summary': '请在后台查看详情', 'event_type': '事件',
    'time': '', 'generation': '', 'buyer_name': '未知买家', 'buyer_id': '',
    'item_id': '', 'chat_id': '', 'message': '请在后台查看消息',
    'result': '请在后台查看履约详情', 'error_message': '请在后台查看事件详情',
    'status_text': '', 'verification_type': '人工验证', 'verification_action': '打开账号页继续验证',
    'verification_url': '/accounts', 'cookie_count': '', 'status_note': '',
    'pause_reason': '请查看账号状态', 'action_hint': '打开账号页查看原因和下一步操作',
}
RECOVERY_EVENTS = {'recovered', 'slider_success', 'password_login_success', 'cookie_refresh_success'}
EVENT_FALLBACKS = {key: 'recovered' if key in RECOVERY_EVENTS else 'account_error'
                   for key in DEFAULTS if key not in {'message', 'delivery', 'test', 'account_error', 'recovered'}}


def template_values(event_type, payload):
    values = {**VARIABLES, **{k: str(v)[:4000] for k, v in payload.items() if k in VARIABLES},
              'event_type': event_type}
    for alias in ('message', 'result', 'error_message'):
        if alias not in payload and 'summary' in payload:
            values[alias] = values['summary']
    if 'summary' not in payload:
        for alias in ('message', 'result', 'error_message'):
            if alias in payload:
                values['summary'] = values[alias]
                break
    # A platform verification URL can contain a live session secret. Notifications
    # point to the account panel; the protected browser session supplies the real URL.
    if 'verification_url' in payload:
        values['verification_url'] = '/accounts'
    return redact_secrets(values)


class NotificationTemplateService:
    def __init__(self, session): self.session = session

    def validate(self, event_type, body):
        if event_type not in DEFAULTS: raise ValueError('事件类型未开放模板')
        if not isinstance(body, str) or not 1 <= len(body) <= 4000: raise ValueError('模板长度应为1至4000')
        try:
            for _, field, spec, conv in Formatter().parse(body):
                if field is not None and (field not in VARIABLES or spec or conv): raise ValueError()
        except (ValueError, KeyError):
            raise ValueError('模板格式错误；仅使用已列出的花括号变量') from None

    def render(self, event_type, body, payload):
        values = template_values(event_type, payload)
        try:
            self.validate(event_type, body)
            return redact_secrets(body.format_map(values)), False
        except (ValueError, KeyError):
            return f"[模板失败] {values['account_id']} {event_type}：请在后台查看详情", True

    async def get(self, owner, event_type):
        return await self.session.scalar(select(NotificationTemplate).where(
            NotificationTemplate.owner_id == owner, NotificationTemplate.event_type == event_type))

    async def effective(self, owner, event_type):
        template = await self.get(owner, event_type)
        if template is None and owner != 0:
            template = await self.get(0, event_type)  # imported global defaults, read-only to ordinary users
        if template is None and event_type in EVENT_FALLBACKS:
            template = await self.get(owner, EVENT_FALLBACKS[event_type])
        return template

    async def render_event(self, owner, event_type, payload):
        template = await self.effective(owner, event_type)
        return self.render(event_type, template.body if template else DEFAULTS.get(event_type, DEFAULTS['account_error']), payload)

    async def save(self, owner, event_type, body):
        self.validate(event_type, body)
        template = await self.get(owner, event_type)
        if template: template.body = body
        else: self.session.add(NotificationTemplate(owner_id=owner, event_type=event_type, body=body))
        await self.session.commit()

    async def reset(self, owner, event_type):
        if event_type not in DEFAULTS: raise ValueError('事件类型未开放模板')
        template = await self.get(owner, event_type)
        if template: await self.session.delete(template)
        await self.session.commit()

    async def list(self, owner):
        rows = (await self.session.scalars(select(NotificationTemplate).where(NotificationTemplate.owner_id.in_([0, owner])))).all()
        global_defaults = {r.event_type: r.body for r in rows if r.owner_id == 0}
        custom = {r.event_type: r.body for r in rows if r.owner_id == owner}
        return [{'event_type': k, 'body': custom.get(k, global_defaults.get(k, v)),
                 'default': global_defaults.get(k, v), 'variables': VARIABLES, 'customized': k in custom}
                for k, v in DEFAULTS.items()]
