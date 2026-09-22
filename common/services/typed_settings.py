"""Typed system/user defaults with explicit account inheritance and version fencing.

Legacy account columns remain explicit until a user chooses inheritance. Effective
values are projected into those same columns/runtime values in one SQL transaction;
existing consumers do not acquire a parallel configuration source.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from common.models.system_setting import SystemSetting
from common.models.user_setting import UserSetting
from common.models.xy_account import XYAccount
from common.models.admin_control import AdminAudit
from common.services import account_policy as policy
from common.services.account_configuration import ConfigurationError, RequestPolicyUpdate
from common.services.account_request_budget import account_risk_config

SETTINGS_KEY = 'xymb.account.defaults'


@dataclass(frozen=True)
class Definition:
    label: str
    type: str
    default: Any
    minimum: int | None = None
    maximum: int | None = None
    nullable: bool = False


DEFINITIONS = {
    'pause_duration': Definition('人工回复后暂停分钟', 'integer', 10, 0, 3600),
    'reply_delay_seconds': Definition('自动回复延迟秒数', 'integer', 0, 0, 3600),
    'message_expire_time': Definition('相同消息等待秒数', 'integer', 3600, 0, 86400),
    'auto_polish': Definition('自动擦亮', 'boolean', False),
    'scheduled_rate': Definition('定时评价', 'boolean', False),
    'auto_red_flower': Definition('自动求小红花', 'boolean', False),
    'show_browser': Definition('显示登录浏览器', 'boolean', False),
    'delivery_disabled_reason': Definition('买家信誉规则回复说明', 'string', '', maximum=500, nullable=True),
    'risk': Definition('账号共享业务频率', 'object', None, nullable=True),
}


class SettingUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    scope: Literal['account', 'user', 'system']
    key: str = Field(min_length=1, max_length=80)
    action: Literal['set', 'inherit'] = 'set'
    value: Any = None
    expected_version: int = Field(ge=0)


def validate_value(key, value):
    if key not in DEFINITIONS:
        raise ValueError('unknown_configuration_key')
    field = DEFINITIONS[key]
    if value is None and field.nullable:
        return None
    if field.type == 'boolean' and type(value) is bool:
        return value
    if field.type == 'integer' and type(value) is int and field.minimum <= value <= field.maximum:
        return value
    if field.type == 'string' and isinstance(value, str) and len(value) <= field.maximum:
        return value
    if key == 'risk' and isinstance(value, dict):
        return RequestPolicyUpdate(expected_config_version=0, **value).risk_values()
    raise ValueError('invalid_configuration_value')


def clear_inheritance(account, keys):
    state = policy.snapshot(account)
    state['inherited_fields'] = sorted(set(state.get('inherited_fields', [])) - set(keys))
    sources = dict(state.get('config_sources', {}))
    for key in keys:
        sources.pop(key, None)
    state['config_sources'] = sources
    policy.store(account, state)


def _decode(row):
    if row is None:
        return {'version': 0, 'values': {}}
    try:
        value = json.loads(row.value)
        if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] < 0
                or not isinstance(value.get('values'), dict)):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ConfigurationError('invalid_configuration_layer') from None


def _value(account, key):
    return account_risk_config(account) if key == 'risk' else getattr(account, key)


def _put(account, key, value):
    if key == 'risk':
        state = policy.snapshot(account)
        state['config_values'] = {**state.get('config_values', {}), 'risk': deepcopy(value)}
        policy.store(account, state)
    else:
        setattr(account, key, value)


def _parent(key, system, user):
    for source, layer in (('user', user), ('system', system)):
        if key in layer['values']:
            return validate_value(key, layer['values'][key]), source
    return deepcopy(DEFINITIONS[key].default), 'default'


class TypedSettings:
    def __init__(self, session):
        self.session = session

    async def _user(self, owner):
        rows = (await self.session.execute(select(UserSetting).where(
            UserSetting.user_id == owner, UserSetting.key == SETTINGS_KEY))).scalars().all()
        if len(rows) > 1:
            raise ConfigurationError('ambiguous_configuration_layer')
        return rows[0] if rows else None

    def _view(self, account, system, user, is_admin):
        state = policy.snapshot(account)
        inherited = set(state.get('inherited_fields', []))
        values = {}
        for key in DEFINITIONS:
            value, source = _parent(key, system, user) if key in inherited else (_value(account, key), 'account')
            try:
                value = validate_value(key, value)
                values[key] = {'value': value, 'source': source, 'inherited': key in inherited}
            except ValueError:
                values[key] = {'value': None, 'source': source, 'inherited': key in inherited,
                               'error': 'invalid_legacy_value'}
        return {'account_id': account.account_id, 'schema': {key: asdict(value) for key, value in DEFINITIONS.items()},
                'values': values, 'can_edit_system': is_admin,
                'versions': {'system': system['version'], 'user': user['version'], 'account': state['config_version']},
                'scope_values': {name: {key: value for key, value in layer['values'].items() if key in DEFINITIONS}
                                 for name, layer in (('system', system), ('user', user))},
                'pending_consumers': policy.pending_consumers(account),
                'unknown_keys': sorted((set(system['values']) | set(user['values'])) - set(DEFINITIONS))}

    async def read(self, account, is_admin=False):
        system = await self.session.get(SystemSetting, SETTINGS_KEY)
        return self._view(account, _decode(system), _decode(await self._user(account.owner_id)), is_admin)

    def _project(self, account, system, user, *, force=False):
        state = policy.snapshot(account)
        sources = dict(state.get('config_sources', {}))
        changed = force
        for key in state.get('inherited_fields', []):
            if key not in DEFINITIONS:
                continue
            value, source = _parent(key, system, user)
            changed |= _value(account, key) != value or sources.get(key) != source
            _put(account, key, value)
            sources[key] = source
        if changed:
            state = policy.snapshot(account)
            state['config_sources'] = sources
            policy.store(account, state)
            policy.bump_config_preserving_pause(account)
        return changed

    async def _sync_delivery_reason(self, account):
        from common.models.xy_delivery_block_rule import XYDeliveryBlockRule
        rule = await self.session.scalar(select(XYDeliveryBlockRule).where(
            XYDeliveryBlockRule.account_id == account.account_id,
            XYDeliveryBlockRule.rule_code == 'buyer_credit_zero').with_for_update())
        if rule is None:
            # Editing text alone never enables an operational rule.
            rule = XYDeliveryBlockRule(account_id=account.account_id, rule_code='buyer_credit_zero',
                enabled=False, priority=10, config={'threshold': 0})
            self.session.add(rule)
        rule.block_reason = account.delivery_disabled_reason

    async def save(self, account, actor_id, is_admin, payload):
        if payload.scope == 'system' and not is_admin:
            raise ConfigurationError('administrator_required', 403)
        if payload.key not in DEFINITIONS:
            raise ValueError('unknown_configuration_key')
        value = validate_value(payload.key, payload.value) if payload.action == 'set' else None
        # Created by the additive startup migration, not by a web request. Serializes
        # scope changes, including the first per-user defaults row, across processes.
        system_row = await self.session.scalar(select(SystemSetting).where(SystemSetting.key == SETTINGS_KEY)
            .with_for_update().execution_options(populate_existing=True))
        if system_row is None:
            raise ConfigurationError('configuration_migration_required', 503)
        account = await self.session.scalar(select(XYAccount).where(XYAccount.id == account.id)
            .with_for_update().execution_options(populate_existing=True))
        system, user_row = _decode(system_row), await self._user(account.owner_id)
        user = _decode(user_row)
        version = policy.snapshot(account)['config_version'] if payload.scope == 'account' else (
            system['version'] if payload.scope == 'system' else user['version'])
        if version != payload.expected_version:
            raise ConfigurationError('stale_configuration')
        affected = 0
        if payload.scope == 'account':
            state = policy.snapshot(account)
            inherited = set(state.get('inherited_fields', []))
            if payload.action == 'inherit':
                inherited.add(payload.key)
            else:
                inherited.discard(payload.key)
                state.setdefault('config_sources', {}).pop(payload.key, None)
            state['inherited_fields'] = sorted(inherited)
            policy.store(account, state)
            if payload.action == 'set':
                _put(account, payload.key, value)
            affected = int(self._project(account, system, user, force=True))
            if payload.key == 'delivery_disabled_reason':
                await self._sync_delivery_reason(account)
        else:
            layer = system if payload.scope == 'system' else user
            if payload.action == 'set':
                layer['values'][payload.key] = value
            else:
                layer['values'].pop(payload.key, None)
            layer['version'] += 1
            if payload.scope == 'user' and user_row is None:
                user_row = UserSetting(user_id=account.owner_id, key=SETTINGS_KEY, value='')
                self.session.add(user_row)
            row = system_row if payload.scope == 'system' else user_row
            row.value = json.dumps(layer, ensure_ascii=False, allow_nan=False)
            query = select(XYAccount).where(XYAccount.status != 'deleted').order_by(XYAccount.id).with_for_update()
            if payload.scope == 'user':
                query = query.where(XYAccount.owner_id == account.owner_id)
            targets = (await self.session.execute(query.execution_options(populate_existing=True))).scalars().all()
            cache = {account.owner_id: user}
            for target in targets:
                if payload.key not in policy.snapshot(target).get('inherited_fields', []):
                    continue
                if target.owner_id not in cache:
                    cache[target.owner_id] = _decode(await self._user(target.owner_id))
                changed = self._project(target, system, cache[target.owner_id])
                affected += int(changed)
                if changed and payload.key == 'delivery_disabled_reason':
                    await self._sync_delivery_reason(target)
        self.session.add(AdminAudit(actor_id=actor_id, action='configuration.' + payload.scope,
            target=account.account_id if payload.scope == 'account' else payload.scope,
            details={'key': payload.key, 'action': payload.action, 'affected_accounts': affected}))
        await self.session.commit()
        result = self._view(account, system, user, is_admin)
        result['affected_accounts'] = affected
        return result
