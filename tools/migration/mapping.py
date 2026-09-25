"""GuDong snapshot -> existing XY models; gaps stay encrypted and block activation."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from string import Formatter
from passlib.hash import pbkdf2_sha256

from .snapshot import Snapshot, MigrationError, canonical, digest


@dataclass(repr=False)
class Step:
    source: str
    identity: str
    target: str
    values: dict = field(repr=False)
    source_hash: str
    lookup: tuple


@dataclass(repr=False)
class Plan:
    namespace: str
    snapshot: Snapshot = field(repr=False)
    checksum: str
    steps: list[Step] = field(default_factory=list, repr=False)
    issues: list[dict] = field(default_factory=list)
    runtime_state: dict | None = field(default=None, repr=False)

    def report(self):
        return {'namespace': self.namespace, 'checksum': self.checksum,
                'inventory': self.snapshot.inventory(),
                'mapped_counts': dict(Counter(s.target for s in self.steps)),
                'issues': self.issues, 'blocked': any(i['blocking'] for i in self.issues)}


def model_tables():
    # Load actual application metadata, never the configured session/engine.
    import common.models  # noqa: F401
    import common.models.reply_state  # noqa: F401
    import common.models.ai_preset  # noqa: F401
    import common.models.delivery_intent  # noqa: F401
    import common.models.notification_delivery  # noqa: F401
    import common.models.product_feedback  # noqa: F401
    import common.models.product_operation  # noqa: F401
    import common.models.auto_rate_config  # noqa: F401
    import common.models.scheduled_red_flower_log  # noqa: F401
    from common.db.base_class import Base
    return Base.metadata.tables


def timestamp(value):
    if not value:
        return datetime(1970, 1, 1)
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        raise MigrationError('invalid_timestamp') from None


def plan(snapshot: Snapshot, *, namespace: str, key: bytes, source_key=None, runtime_state=None, now=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', namespace):
        raise MigrationError('invalid_namespace')
    from copy import deepcopy
    result = Plan(namespace, snapshot, snapshot.checksum(key), runtime_state=deepcopy(runtime_state))
    tables = model_tables()
    from .legacy import decrypt_rows, runtime_pauses, convert_setting, convert_template, EVENTS, specifications, commerce
    rows = decrypt_rows(snapshot, source_key)
    users = {r['id']: r for r in rows['users']}
    accounts = {r['id']: r for r in rows['cookies']}
    used = {}

    def uid(table, identity):
        return 1 + int(digest([namespace, table, identity], key)[:8], 16) % 2_000_000_000

    def token(table, identity):
        return digest([namespace, table, identity], key)

    def issue(table, code, blocking=True, **extra):
        value = dict(table=table, code=code, blocking=blocking, **extra)
        if value not in result.issues:
            result.issues.append(value)

    def owner(source_id):
        if source_id not in users:
            raise MigrationError('missing_owner_reference')
        return uid('users', source_id)

    def account(source_id):
        if source_id not in accounts:
            raise MigrationError('missing_account_reference')
        return accounts[source_id]

    def add(source, row, target, values, lookup=('id',), identity=None):
        if identity is None:
            index = rows[source].index(row)
            identity = snapshot.identities[source][index]
        step_id = token(source, [identity, target])
        result.steps.append(Step(source, step_id, target, values, digest(row, key), lookup))

    def consume(table, fields):
        used.setdefault(table, set()).update(fields.split())
        used[table].update(('created_at', 'updated_at'))

    def common_times(row):
        return {n: timestamp(row[n]) for n in ('created_at', 'updated_at') if row.get(n)}

    # Validate every known owner/account link before producing any writes.
    for table, entries in rows.items():
        for r in entries:
            global_scope = table in ('xy_personal_blacklist', 'xy_message_filter_rules') and r.get('cookie_id') in (None, '')
            if r.get('cookie_id') is not None and not global_scope:
                a = account(r['cookie_id'])
                if table != 'ai_conversations' and 'user_id' in r and r['user_id'] != a['user_id']:
                    raise MigrationError('cross_owner_reference')
            if table not in ('ai_conversations', 'chat_messages') and r.get('user_id') is not None:
                owner(r['user_id'])
    for r in rows['users']:
        source_hash = r.get('password_hash', '')
        compatible = False
        try:
            compatible = pbkdf2_sha256.identify(source_hash)
            if compatible:
                pbkdf2_sha256.from_string(source_hash)
        except (ValueError, TypeError):
            compatible = False
        # The actual AuthService explicitly supports legacy SHA256 hex hashes.
        compatible = compatible or bool(re.fullmatch(r'[0-9a-fA-F]{64}', source_hash))
        if not compatible:
            # Deterministic, unguessable replacement; migration key is not a user password.
            source_hash = pbkdf2_sha256.using(rounds=29000, salt=bytes.fromhex(token('password-salt', r['id'])[:32])).hash(token('password', r['id']))
            issue('users', 'password_reset_required', False)
        add('users', r, 'xy_users', dict(id=owner(r['id']), username=r['username'], email=r['email'],
            password_hash=source_hash, status='INACTIVE', role='ADMIN' if r.get('is_admin') else 'MEMBER',
            external_id='migration:' + token('users', r['id'])[:40], **common_times(r)), lookup=('username',))
    consume('users', 'id username email password_hash is_active is_admin')

    ai_settings = {r['cookie_id']: r for r in rows.get('ai_reply_settings', [])}
    status = {r['cookie_id']: r for r in rows.get('cookie_status', [])}
    for r in rows['cookies']:
        data = {k: r[k] for k in ('username', 'remark', 'pause_duration', 'auto_confirm', 'show_browser',
                'auto_red_flower', 'proxy_type', 'proxy_host', 'proxy_port', 'proxy_user', 'proxy_pass') if k in r}
        metadata = {'reply_policy': 'legacy', 'migration': {'namespace': namespace, 'original_status': status.get(r['id'], {}).get('enabled'),
                    'original_settings': {k: r[k] for k in ('pause_duration', 'auto_confirm', 'auto_red_flower', 'auto_comment', 'show_browser', 'status_note', 'qr_login_grace_until') if k in r}}}
        if r['id'] in ai_settings:
            settings = {k: v for k, v in ai_settings[r['id']].items() if k in ('ai_enabled', 'api_type', 'api_key', 'model_name', 'base_url', 'max_bargain_rounds', 'max_discount_percent', 'max_discount_amount', 'custom_prompts')}
            from common.services.ai_gateway import canonical_provider
            try:
                settings['provider_type'] = canonical_provider(settings.get('api_type'))
            except ValueError:
                issue('ai_reply_settings', 'unsupported_ai_protocol')
                settings['provider_type'] = settings.get('api_type')
            metadata['ai_reply_settings'] = settings
        schedules=[task for task in rows.get('scheduled_tasks',[]) if task.get('account_id')==r['id'] and task.get('task_type')=='item_polish']
        if len(schedules)==1:
            data['auto_polish']=bool(schedules[0].get('enabled',True))
            metadata['migration']['original_settings']['auto_polish']=data['auto_polish']
        add('cookies', r, 'xy_accounts', dict(id=uid('cookies', r['id']), owner_id=owner(r['user_id']),
            account_id=r['id'], cookie=r['value'], login_password=r.get('password', ''), login_method='cookie',
            status='disabled', disable_reason='migration_quarantine', metadata=metadata, **data, **common_times(r)), lookup=('account_id',))
        add('cookies', r, 'xy_reply_policies', dict(account_id=r['id'], strategy='legacy', version=1), lookup=('account_id',))
        for field_name in ('value', 'password', 'proxy_pass'):
            if str(r.get(field_name, '')).startswith(('enc$', 'enc:')):
                issue('cookies', 'encrypted_legacy_secret_requires_adapter')
    consume('cookies', 'id user_id value password username remark pause_duration auto_confirm auto_comment show_browser auto_red_flower proxy_type proxy_host proxy_port proxy_user proxy_pass status_note qr_login_grace_until')
    consume('cookie_status', 'cookie_id enabled')
    consume('ai_reply_settings', 'cookie_id ai_enabled api_type api_key model_name base_url max_bargain_rounds max_discount_percent max_discount_amount custom_prompts')

    for r in rows.get('keywords', []):
        a = account(r['cookie_id'])
        identity = [r['cookie_id'], r['keyword'], r.get('item_id') or None]
        add('keywords', r, 'xy_keyword_rules', dict(id=uid('keywords', identity), owner_id=owner(a['user_id']),
            account_id=uid('cookies', a['id']), keyword=r['keyword'], reply_content=r.get('reply'),
            reply_type=r.get('type', 'text'), image_url=r.get('image_url'), item_id=r.get('item_id') or None,
            is_active=True), lookup=('account_id', 'keyword', 'item_id'))
        if r.get('image_url'):
            issue('keywords', 'attachment_verification_required')
    consume('keywords', 'cookie_id keyword reply item_id type image_url')
    for r in rows.get('default_replies', []):
        add('default_replies', r, 'xy_default_replies', dict(id=uid('defaults', r['cookie_id']), account_id=r['cookie_id'], item_id=None,
            enabled=bool(r.get('enabled', False)), reply_content=r.get('reply_content'), reply_once=bool(r.get('reply_once', False)),
            **common_times(r)), lookup=('account_id', 'item_id'))
    consume('default_replies', 'cookie_id enabled reply_content reply_once')
    from common.services.reply_state import identity as reply_identity, normalize_chat
    for r in rows.get('item_replay', []):
        add('item_replay', r, 'xy_exclusive_replies', dict(id=token('exclusive', r['id']), account_id=r['cookie_id'],
            item_id=r['item_id'], content=r['reply_content'], enabled=True), lookup=('account_id', 'item_id'))
    consume('item_replay', 'id cookie_id item_id reply_content')
    for r in rows.get('default_reply_records', []):
        chat = normalize_chat(r['chat_id'])
        add('default_reply_records', r, 'xy_reply_once_slots', dict(id=reply_identity(r['cookie_id'], chat, ''),
            account_id=r['cookie_id'], chat_id=chat, scope='', request_id='migration:' + token('once', r['id'])[:40], status='confirmed'))
    consume('default_reply_records', 'id cookie_id chat_id replied_at')
    for r in rows.get('ai_conversations', []):
        add('ai_conversations', r, 'xy_ai_chat_messages', dict(id=uid('ai_conversations', r['id']),
            **{k: r.get(k) for k in ('cookie_id', 'user_id', 'item_id', 'role', 'content', 'intent')},
            chat_id=normalize_chat(r['chat_id']), created_at=timestamp(r.get('created_at'))))
        candidates = [x for x in rows.get('chat_messages', []) if x['cookie_id'] == r['cookie_id']
                      and normalize_chat(x['chat_id']) == normalize_chat(r['chat_id'])
                      and x.get('content') == r.get('content')
                      and timestamp(x.get('created_at')) == timestamp(r.get('created_at'))]
        if candidates:
            # No platform message identity exists in GuDong AI rows. Do not fabricate
            # a content-based dedup decision: preserve both archives and block activation.
            issue('ai_conversations', 'ambiguous_chat_history_overlap')
        else:
            event_id = 'migration-ai:' + token('ai_event', r['id'])
            add('ai_conversations', r, 'xy_reply_events', dict(account_id=r['cookie_id'],
                chat_id=normalize_chat(r['chat_id']), event_id=event_id, message_id=event_id,
                role=r['role'], origin='ai', sender_id=r['user_id'] if r['role'] == 'user' else '',
                item_id=r.get('item_id') or '', content=r['content'], content_type='text', status='confirmed',
                occurred_at=timestamp(r.get('created_at')).replace(tzinfo=timezone.utc).timestamp()),
                lookup=('account_id', 'chat_id', 'event_id'))
        if r.get('bargain_count', 0):
            issue('ai_conversations', 'bargain_count_semantics_requires_review')
    consume('ai_conversations', 'id cookie_id chat_id user_id item_id role content intent bargain_count')
    # Chat history is a separate target; do not merge heuristic duplicates with AI history.
    for r in sorted(rows.get('chat_messages', []), key=lambda r: (timestamp(r.get('created_at')), r['id'])):
        direction = int(r.get('direction', 2))
        if direction not in (1, 2):
            raise MigrationError('invalid_message_direction')
        content_type = r.get('content_type', 1)
        system_notice = content_type in (14, 25, 26)
        if content_type not in (1, 2, 14, 25, 26):
            issue('chat_messages', 'unsupported_message_type')
        add('chat_messages', r, 'xy_reply_events', dict(account_id=r['cookie_id'], chat_id=normalize_chat(r['chat_id']), event_id='migration:' + token('chat', r['id']), message_id='migration:' + token('chat', r['id']),
            role='system' if system_notice else ('assistant' if direction == 1 else 'user'), origin='manual' if r.get('reply_source') == 'manual' else 'platform',
            sender_id=r.get('sender_id') or '', sender_name=r.get('sender_name') or '', item_id=r.get('item_id') or '',
            content=(r.get('image_url') or r.get('content') or '') if content_type==2 else (r.get('content') or ''), content_type='system' if system_notice else ('image' if content_type == 2 else 'text'),
            occurred_at=timestamp(r.get('created_at')).replace(tzinfo=timezone.utc).timestamp(), status='confirmed'),
            lookup=('account_id', 'chat_id', 'event_id'))
        if r.get('image_url') or r.get('media_url') or r.get('extra_json'):
            issue('chat_messages', 'rich_message_attachment_requires_review')
    consume('chat_messages', 'id cookie_id chat_id sender_id sender_name content content_type image_url item_id direction reply_source media_url link_url extra_json')

    for r in runtime_pauses(runtime_state, namespace=namespace, checksum=result.checksum, accounts=accounts, now=now):
        chat = normalize_chat(r['chat_id'])
        add('runtime_pauses', r, 'xy_reply_pauses', dict(id=reply_identity(r['account_id'], chat),
            account_id=r['account_id'], chat_id=chat, until=r['until'], version=1), identity=[r['account_id'], chat])

    channels = {r['id']: r for r in rows.get('notification_channels', [])}
    inferred_owners = {}
    for r in rows.get('message_notifications', []):
        if r['channel_id'] not in channels:
            raise MigrationError('missing_channel_reference')
        o = account(r['cookie_id'])['user_id']
        c = channels[r['channel_id']]
        if c.get('user_id', o) != o or inferred_owners.get(c['id'], o) != o:
            raise MigrationError('cross_owner_reference')
        inferred_owners[c['id']] = o
    for r in channels.values():
        source_owner = r.get('user_id', inferred_owners.get(r['id']))
        if source_owner is None:
            issue('notification_channels', 'missing_channel_owner')
            continue
        try:
            config = json.loads(r['config'])
            if not isinstance(config, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise MigrationError('invalid_channel_config') from None
        add('notification_channels', r, 'xy_notification_channels', dict(id=uid('channels', r['id']),
            owner_id=owner(source_owner), name=r['name'], channel_type=r['type'], config=config,
            enabled=bool(r.get('enabled', True)), **common_times(r)))
    consume('notification_channels', 'id user_id name type config enabled')
    for r in rows.get('message_notifications', []):
        a = account(r['cookie_id'])
        add('message_notifications', r, 'xy_message_notifications', dict(id=uid('notifications', r['id']),
            owner_id=owner(a['user_id']), account_pk=uid('cookies', a['id']), account_identifier=a['id'],
            channel_id=uid('channels', r['channel_id']), enabled=bool(r.get('enabled', True)), **common_times(r)),
            lookup=('account_identifier', 'channel_id'))
    consume('message_notifications', 'id cookie_id channel_id enabled')

    for r in rows.get('item_info', []):
        a = account(r['cookie_id'])
        add('item_info', r, 'xy_catalog_items', dict(id=uid('items', r['id']), owner_id=owner(a['user_id']),
            account_id=uid('cookies', a['id']), item_id=r['item_id'], title=r.get('item_title'), price=r.get('item_price'),
            metadata={'migration': {'source_identity': token('item', r['id'])}, 'is_multi_spec': bool(r.get('is_multi_spec', False)), 'multi_quantity_delivery': bool(r.get('multi_quantity_delivery', False))}, created_at=timestamp(r.get('created_at'))), lookup=('account_id', 'item_id'))
        # Native legacy title rules read these item flags when calculating units.
    consume('item_info', 'id cookie_id item_id item_title item_description item_category item_price item_detail is_multi_spec multi_quantity_delivery')
    for r in rows.get('cards', []):
        # Disabled until reservations and finalization can be represented by the fulfillment target.
        values = {k: v for k, v in r.items() if k in tables['xy_cards'].c and k not in ('id', 'user_id', 'enabled', 'created_at', 'updated_at')}
        values.update(specifications(r))
        add('cards', r, 'xy_cards', dict(id=uid('cards', r['id']), user_id=owner(r['user_id']), enabled=False,
            **values, **common_times(r)))
        if r.get('image_url'):
            issue('cards', 'attachment_verification_required')
    consume('cards', ' '.join(tables['xy_cards'].c.keys()) + ' spec_name_2 spec_value_2')
    order_index = {(r['cookie_id'], r['order_id']): r for r in rows.get('orders', [])}
    for source in ('data_card_reservations', 'delivery_finalization_states'):
        for r in rows.get(source, []):
            if (r.get('cookie_id'), r.get('order_id')) not in order_index:
                raise MigrationError('missing_order_reference')
            if source == 'data_card_reservations':
                card = next((c for c in rows.get('cards', []) if c['id'] == r['card_id']), None)
                if card is None:
                    raise MigrationError('missing_card_reference')
                if card['user_id'] != account(r['cookie_id'])['user_id']:
                    raise MigrationError('cross_owner_reference')
        if rows.get(source):
            issue(source, 'unit_lifecycle_reconciliation_required')
    for r in rows.get('orders', []):
        a = account(r['cookie_id'])
        try:
            quantity = int(str(r.get('quantity', '1')))
            amount = Decimal(str(r.get('amount') if r.get('amount') is not None else 0))
            if quantity < 1 or not amount.is_finite():
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise MigrationError('invalid_order_number') from None
        original_status = r.get('order_status')
        terminal = {'completed': 'completed', 'cancelled': 'cancelled', 'refunding': 'refunding', 'shipped': 'shipped'}
        facts = {name: [x for x in rows.get(name, []) if x.get('cookie_id') == a['id'] and x.get('order_id') == r['order_id']]
                 for name in ('data_card_reservations', 'delivery_finalization_states')}
        add('orders', r, 'xy_orders', dict(id=uid('orders', [a['id'], r['order_id']]), owner_id=owner(a['user_id']),
            account_id=a['id'], order_no=r['order_id'], status=terminal.get(original_status, 'pending_verification'),
            item_id=r.get('item_id'), buyer_id=r.get('buyer_id'), buyer_nick=r.get('buyer_nick'), chat_id=r.get('sid'),
            quantity=quantity, amount=amount, **specifications(r),
            is_rated=bool(r.get('is_rated', False)), is_red_flower=bool(r.get('is_red_flower', False)), card_only_delivered=True,
            source='migration', metadata={'migration': {'platform_status': original_status, 'source_identity': token('order', [r['cookie_id'], r['order_id']]), 'pre_refund_status': r.get('pre_refund_status'), 'fact_counts': {name: len(entries) for name, entries in facts.items()}, 'archive_namespace': namespace}}, **common_times(r)),
            lookup=('account_id', 'order_no'))
    consume('orders', 'order_id cookie_id item_id buyer_id buyer_nick sid order_status quantity amount is_rated is_red_flower spec_name spec_value spec_name_2 spec_value_2 bargain_flow_detected bargain_success_detected pre_refund_status platform_created_at platform_paid_at platform_completed_at rated_at rate_error red_flower_at red_flower_error')

    for r in rows.get('ai_config_presets', []):
        from common.services.ai_gateway import canonical_provider
        settings = {k: r.get(k, '') for k in ('model_name', 'api_key', 'base_url')}
        try:
            settings['provider_type'] = canonical_provider(r.get('api_type'))
        except ValueError:
            issue('ai_config_presets', 'unsupported_ai_protocol')
            continue
        add('ai_config_presets', r, 'xy_ai_presets', dict(id=token('preset', r['id'])[:32],
            owner_id=owner(r['user_id']), name=r['preset_name'], settings_json=settings, **common_times(r)))
    consume('ai_config_presets', 'id user_id preset_name model_name api_key base_url api_type')
    for source in ('xy_personal_blacklist', 'xy_platform_blacklist'):
        for r in rows.get(source, []):
            values = dict(id=uid(source, r['id']), owner_id=owner(r['user_id']),
                          buyer_id=r['buyer_id'], buyer_nick=r.get('buyer_nick'), **common_times(r))
            if source == 'xy_personal_blacklist':
                values.update(account_id=r.get('cookie_id') or None, item_id=r.get('item_id') or None,
                              reason=r.get('reason'), is_enabled=bool(r.get('is_enabled', True)))
            add(source, r, source, values)
        consume(source, 'id user_id cookie_id buyer_id buyer_nick item_id reason is_enabled')

    for r in rows.get('xy_message_filter_rules', []):
        from common.services.reply_policy import validate_filter
        try:
            patterns = json.loads(r['patterns'])
            if not isinstance(patterns, list) or not patterns:
                raise ValueError()
        except (ValueError, TypeError):
            issue('xy_message_filter_rules', 'invalid_filter_patterns')
            continue
        targets = [r['cookie_id']] if r.get('cookie_id') else ['']
        actions = [action for key_name, action in (('action_skip_auto_reply', 'skip_reply'),
            ('action_skip_ai_reply', 'skip_ai'), ('action_notify', 'notify'),('action_pause_minutes','pause')) if r.get(key_name, 0)]
        for account_id in targets:
            for n, pattern in enumerate(patterns):
                values = dict(id=token('filter', [r['id'], account_id, n]), account_id=account_id,
                    owner_id=owner(r['user_id']), pause_minutes=r.get('action_pause_minutes',0),
                    pattern=pattern, match_mode=r.get('match_type', 'contains'), source=r.get('message_source', 'user'),
                    item_id=r.get('item_id') or '', actions=actions, enabled=bool(r.get('is_enabled', True)))
                try:
                    validate_filter(values)
                except (ValueError, TypeError):
                    issue('xy_message_filter_rules', 'filter_semantics_required')
                    continue
                add('xy_message_filter_rules', r, 'xy_reply_filters', values, identity=[r['id'], account_id, n])
    consume('xy_message_filter_rules', 'id user_id cookie_id item_id name match_type patterns message_source is_enabled action_skip_auto_reply action_skip_ai_reply action_pause_minutes action_notify')
    for r in rows.get('notification_templates', []):
        try:
            event_type, body = convert_template(r['type'], r['template'])
        except MigrationError:
            issue('notification_templates', 'notification_template_semantics_required')
            continue
        if sum(1 for x in rows.get('notification_templates', [])
               if EVENTS.get(x['type'], x['type']) == event_type) > 1:
            issue('notification_templates', 'notification_event_collision')
            continue
        for owner_id in [0, *users]:
            add('notification_templates', r, 'xy_notify_templates', dict(
                id=token('notify_template', [r['id'], owner_id])[:36], owner_id=owner(owner_id) if owner_id else 0,
                event_type=event_type, body=body, **common_times(r)),
                lookup=('owner_id', 'event_type'), identity=[r['id'], owner_id])
        # owner 0 is the read-only inherited default for future users.
    consume('notification_templates', 'id type template')

    # One existing fulfillment intent per order blocks payment-event replay. Partial unit
    # evidence remains unknown, never promoted to an all-units-sent result.
    card_index = {r['id']: r for r in rows.get('cards', [])}
    for r in rows.get('orders', []):
        reservations = [x for x in rows.get('data_card_reservations', [])
                        if x.get('cookie_id') == r['cookie_id'] and x.get('order_id') == r['order_id'] and x.get('status') != 'released']
        finalizations = [x for x in rows.get('delivery_finalization_states', [])
                         if x.get('cookie_id') == r['cookie_id'] and x.get('order_id') == r['order_id']]
        if not reservations:
            continue
        card_ids = {x['card_id'] for x in reservations}
        if len(card_ids) != 1:
            issue('data_card_reservations', 'multi_card_intent_adapter_required')
            continue
        card_id = next(iter(card_ids)); card = card_index[card_id]
        qty = int(r.get('quantity', 1))
        units = [x['unit_index'] for x in reservations]
        if len(units) != len(set(units)) or any(not isinstance(x, int) or x < 1 or x > qty for x in units):
            raise MigrationError('invalid_reservation_units')
        finalized_units = {x['unit_index'] for x in finalizations if x.get('status') == 'finalized'}
        sent_units = {x['unit_index'] for x in finalizations if x.get('status') in ('sent', 'finalized')}
        full_sent = set(range(1, qty + 1)) <= sent_units
        full_confirmed = set(range(1, qty + 1)) <= finalized_units
        lines = [x['reserved_content'] for x in sorted(reservations, key=lambda x: x['unit_index'])]
        # GuDong already removed reserved lines from its free pool. Inconsistent snapshots stop.
        if set(lines) & {x.strip() for x in (card.get('data_content') or '').splitlines() if x.strip()}:
            raise MigrationError('reserved_card_in_free_pool')
        values = dict(id=token('delivery_intent', [r['cookie_id'], r['order_id']])[:36],
            owner_id=owner(account(r['cookie_id'])['user_id']), account_id=r['cookie_id'], order_no=r['order_id'],
            operation_key='payment', card_id=uid('cards', card_id), card_type=card['type'], quantity=qty,
            mode='send_first', content_state='confirmed' if full_sent else 'unknown',
            confirm_state='confirmed' if full_confirmed else 'unknown', reserved_lines=lines,
            payload={'texts': lines, 'images': []}, last_error='migration_reconciliation_required',
            evidence=[{'source': 'migration', 'unit_index': x['unit_index'], 'status': x.get('status')} for x in finalizations])
        add('orders', r, 'xy_delivery_intents', values, lookup=('owner_id', 'account_id', 'order_no', 'operation_key'))

    materials = {r['id']: r for r in rows.get('product_materials', [])}
    for r in materials.values():
        try:
            images = json.loads(r.get('images') or '[]')
            sku = json.loads(r.get('sku_config') or '{}')
            if not isinstance(images, list) or not isinstance(sku, dict):
                raise ValueError()
            price = Decimal(str(r.get('price') if r.get('price') is not None else 0))
            if not price.is_finite():
                raise ValueError()
        except (ValueError, TypeError, InvalidOperation):
            raise MigrationError('invalid_material_json_or_price') from None
        specifications, sku_rows = [], []
        if sku.get('enabled'):
            props, items = sku.get('properties'), sku.get('items')
            if not isinstance(props, list) or not isinstance(items, list):
                raise MigrationError('invalid_sku_structure')
            try:
                for prop in props:
                    values = []
                    for v in prop['values']:
                        image = v.get('image') if isinstance(v, dict) else None
                        image_url = next((image[k] for k in ('url', 'image_url', 'src') if image.get(k)), None) if isinstance(image, dict) else None
                        if image and not image_url:
                            issue('product_materials', 'embedded_sku_image_requires_adapter')
                        values.append({'name': v['value'] if isinstance(v, dict) else v, 'image': image_url})
                    specifications.append({'name': prop['name'], 'values': values, 'support_image': bool(prop.get('support_image', False))})
                for item in items:
                    if len(item['values']) != len(props):
                        raise ValueError()
                    sku_rows.append({'specs': dict(zip([p['name'] for p in props], item['values'])),
                                     'price': item['price'], 'stock': item['quantity']})
            except (KeyError, TypeError, ValueError):
                raise MigrationError('invalid_sku_structure') from None
            if len(items) > 200 or any(x['price'] <= 0 for x in sku_rows):
                issue('product_materials', 'target_sku_edit_limits_require_review')
        choice = r.get('delivery_method') or '包邮'
        modes = {'包邮': ('express', 'free'), '自提': ('pickup', 'none'), '不包邮': ('express', 'fixed')}
        if choice not in modes:
            issue('product_materials', 'shipping_mode_mapping_required')
        delivery, shipping = modes.get(choice, ('express', 'free'))
        values = {k: r[k] for k in ('title', 'description', 'original_price', 'category', 'postage', 'brand', 'condition', 'remark') if k in r}
        add('product_materials', r, 'xy_product_materials', dict(id=uid('materials', r['id']), user_id=owner(r['user_id']),
            price=price, images=images, specifications=specifications, sku_rows=sku_rows,
            delivery_method=delivery, shipping_method=shipping, support_pickup=bool(r.get('can_self_pickup', False)),
            **values, **common_times(r)))
        if price <= 0 or not images:
            issue('product_materials', 'target_material_edit_limits_require_review')
        if images or any(p['support_image'] for p in specifications):
            issue('product_materials', 'attachment_verification_required')
    consume('product_materials', 'id user_id title description price original_price category images delivery_method postage can_self_pickup brand condition remark sku_config')
    for r in rows.get('publish_logs', []):
        a = account(r['account_id'])
        if a['user_id'] != r['user_id']:
            raise MigrationError('cross_owner_reference')
        material_id = None
        if r.get('material_id') is not None:
            material = materials.get(r['material_id'])
            if not material:
                raise MigrationError('missing_material_reference')
            if material['user_id'] != r['user_id']:
                raise MigrationError('cross_owner_reference')
            material_id = uid('materials', r['material_id'])
        values = {k: r[k] for k in ('title', 'description', 'price', 'item_url', 'item_id') if k in r}
        status = r.get('status') if r.get('status') in ('success', 'failed') else 'unknown'
        add('publish_logs', r, 'xy_publish_logs', dict(id=uid('publish_logs', r['id']), user_id=owner(r['user_id']),
            account_id=r['account_id'], material_id=material_id, status=status,
            batch_id=token('publish_batch', r.get('batch_id'))[:36] if r.get('batch_id') else None,
            publish_request_id='migration:' + token('publish', r['id']),
            error_message='migration_source_error_archived' if r.get('error_message') else None,
            **values, **common_times(r)))
        issue('publish_logs', 'historical_material_snapshot_requires_review')
    consume('publish_logs', 'id user_id account_id title description price material_id batch_id status item_url item_id error_message')

    commerce(rows, add=add, consume=consume, uid=uid, token=token, owner=owner, account=account,
             issue=issue, common_times=common_times)

    for source in ('user_settings', 'system_settings'):
        for r in rows.get(source, []):
            converted = convert_setting(r['key'], r['value'])
            if converted is None:
                issue(source, 'setting_key_review_required', key_hash=token('setting_key',r['key']))
                continue
            setting_key, value = converted
            values = {'key':setting_key, 'value':value}
            if source == 'user_settings':
                values.update(id=uid(source,r['id']),user_id=owner(r['user_id']))
            add(source,r,'xy_'+source,values,lookup=('user_id','key') if source=='user_settings' else ('key',))
        consume(source, 'id user_id key value description')
    for source, entries in rows.items():
        if source not in used:
            if entries:
                issue(source, 'unmapped_table')
        else:
            extra = set(snapshot.inventory()[source]['columns']) - used[source]
            if extra:
                issue(source, 'unmapped_columns', fields=sorted(extra))
    # One ordered stream for AI and manual history; native autoincrement cursors
    # are assigned in source chronology, never from random identity hashes.
    events = sorted((s for s in result.steps if s.target == 'xy_reply_events'),
                    key=lambda s: (s.values['occurred_at'], s.identity))
    result.steps = [s for s in result.steps if s.target != 'xy_reply_events'] + events
    # Target field validation catches schema drift instead of creating substitute business tables.
    for step in result.steps:
        if step.target not in tables or set(step.values) - set(tables[step.target].c.keys()):
            raise MigrationError('target_schema_mismatch')
        for column in tables[step.target].c:
            if column.name not in step.values:
                if not column.nullable and column.default is None and column.server_default is None and not (column.primary_key and column.autoincrement):
                    raise MigrationError('target_required_field_missing')
                continue
            value = step.values[column.name]
            if value is None and not column.nullable:
                raise MigrationError('target_required_value_missing')
            length = getattr(column.type, 'length', None)
            if isinstance(value, str) and length is not None and len(value) > length:
                raise MigrationError('target_value_too_long')
    return result
