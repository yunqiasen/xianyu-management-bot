"""Explicit additive fork migrations. Executed under the startup DDL lock.

No request creates tables. A migration failure aborts backend startup rather
than exposing endpoints backed by only half a schema.
"""
from common.models.ai_preset import AIPreset
from common.models.reply_state import TABLES as REPLY_TABLES
from common.models.product_reliability_migration import upgrade_products
from common.models.delivery_intent import DeliveryIntent
from common.models.delivery_rule import DeliveryRule
from common.models.order_sync_job import OrderSyncJob
from common.models.admin_control import LoginProtection, LoginProtectionConfig, AdminAudit, BackupVerification, DataPreview, AdminLogArchive, AdminAuditArchive
from common.models.notification_delivery import NotificationEvent, NotificationDelivery, NotificationTemplate

from common.models.account_operation import AccountOperation

FORK_MODELS = [AdminAuditArchive, DeliveryRule, AccountOperation, AdminLogArchive, AIPreset, DeliveryIntent, OrderSyncJob, LoginProtection, LoginProtectionConfig, AdminAudit,
               BackupVerification, DataPreview, NotificationEvent, NotificationDelivery,
               NotificationTemplate]


def upgrade_fork_schema(connection):
    upgrade_account_identity(connection)
    for model in FORK_MODELS:
        model.__table__.create(connection, checkfirst=True)
    for table in REPLY_TABLES:
        table.create(connection, checkfirst=True)
    upgrade_products(connection)
    from sqlalchemy import inspect, text
    columns = {c['name'] for c in inspect(connection).get_columns('xy_notify_events')}
    if 'first_seen' not in columns:
        connection.execute(text('ALTER TABLE xy_notify_events ADD COLUMN first_seen DOUBLE'))
        connection.execute(text('UPDATE xy_notify_events SET first_seen = last_seen WHERE first_seen IS NULL'))
        if connection.dialect.name == 'mysql':
            connection.execute(text('ALTER TABLE xy_notify_events MODIFY COLUMN first_seen DOUBLE NOT NULL'))


    schema = inspect(connection)
    for table, column, definition in [
        ('xy_users', 'token_version', 'INTEGER NOT NULL DEFAULT 0'),
        ('xy_delivery_intents', 'source_snapshot', 'JSON NULL'),
        ('xy_delivery_intents', 'line_id', "VARCHAR(80) NOT NULL DEFAULT 'main'"),
    ]:
        if schema.has_table(table) and column not in {c['name'] for c in schema.get_columns(table)}:
            connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {definition}'))

    from common.models.reply_state import upgrade_reply_schema
    from common.models.account_operation import upgrade_account_operations
    upgrade_reply_schema(connection)
    upgrade_account_operations(connection)
    from common.models.listing_monitor_migration import upgrade_monitor_schema
    from common.models.bargaining_event import BargainingEvent
    upgrade_monitor_schema(connection)
    BargainingEvent.__table__.create(connection, checkfirst=True)
    from common.models.identity import upgrade_identity_columns
    upgrade_identity_columns(connection, [BargainingEvent.__table__])
    upgrade_epoch_precision(connection)
    if inspect(connection).has_table('xy_system_settings'):
        key = 'xymb.account.defaults'
        if connection.execute(text('SELECT 1 FROM xy_system_settings WHERE `key`=:key'), {'key': key}).first() is None:
            connection.execute(text('INSERT INTO xy_system_settings (`key`, value) VALUES (:key, :value)'),
                {'key': key, 'value': '{"version":0,"values":{}}'})


def upgrade_epoch_precision(connection):
    """Widen legacy epoch columns without rewriting saved business facts."""
    if connection.dialect.name != 'mysql':
        return
    from sqlalchemy import inspect
    from sqlalchemy.dialects.mysql import DOUBLE
    fields = {
        'xy_bargaining_events': ('occurred_at',),
        'xy_reply_events': ('occurred_at',),
        'xy_reply_pauses': ('until',),
        'xy_admin_login_protection': ('locked_until',),
        'xy_admin_data_preview': ('expires_at',),
        'xy_notify_events': ('first_seen', 'last_seen'),
        'xy_notify_deliveries': ('due_at',),
        'xy_listing_monitor_state': ('lease_until',),
    }
    schema = inspect(connection)
    quote = connection.dialect.identifier_preparer.quote
    for table, names in fields.items():
        if not schema.has_table(table):
            continue
        columns = {column['name']: column for column in schema.get_columns(table)}
        for name in names:
            column = columns.get(name)
            if column is None or isinstance(column['type'], DOUBLE):
                continue
            nullable = 'NULL' if column['nullable'] else 'NOT NULL'
            connection.exec_driver_sql(
                f'ALTER TABLE {quote(table)} MODIFY COLUMN {quote(name)} DOUBLE {nullable}')


def upgrade_account_identity(connection):
    """Preflight legacy aliases before adding the durable platform-identity constraint."""
    from sqlalchemy import inspect, text
    schema = inspect(connection)
    if not schema.has_table('xy_accounts'):
        return
    columns = {c['name'] for c in schema.get_columns('xy_accounts')}
    if 'unb' not in columns:
        return
    from common.utils.xianyu_utils import trans_cookies
    fields = 'id, unb, cookie' if 'cookie' in columns else 'id, unb'
    rows = connection.execute(text('SELECT ' + fields + ' FROM xy_accounts')).mappings().all()
    seen, changes = {}, []
    for row in rows:
        stored = str(row['unb'] or '').strip() or None
        cookie_identity = trans_cookies(row['cookie']).get('unb') if row.get('cookie') else None
        if stored and cookie_identity and stored != cookie_identity:
            raise ValueError('platform_identity_mismatch')
        identity = stored or cookie_identity or None
        if identity is not None and identity in seen:
            raise ValueError('duplicate_platform_identity')
        if identity is not None:
            seen[identity] = row['id']
        if identity != row['unb']:
            changes.append({'id':row['id'], 'identity':identity})
    if changes:
        connection.execute(text('UPDATE xy_accounts SET unb=:identity WHERE id=:id'), changes)
    unique = any(i.get('unique') and i.get('column_names') == ['unb'] for i in schema.get_indexes('xy_accounts'))
    if not unique:
        connection.execute(text('CREATE UNIQUE INDEX uk_account_unb ON xy_accounts (unb)'))
