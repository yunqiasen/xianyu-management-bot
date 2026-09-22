"""回复领域增量表。由迁移创建；运行请求从不自动建表。"""
from sqlalchemy import Table, Column, String, Integer, BigInteger, Text, JSON, Double, Boolean, UniqueConstraint, Index
from common.db.base_class import Base
from common.models.identity import identity_string, upgrade_identity_columns

metadata = Base.metadata
pk_type = BigInteger().with_variant(Integer, 'sqlite')

reply_events = Table('xy_reply_events', metadata,
    Column('cursor', pk_type, primary_key=True, autoincrement=True),
    Column('account_id', identity_string(80), nullable=False), Column('chat_id', identity_string(128), nullable=False),
    Column('event_id', identity_string(128), nullable=False), Column('message_id', identity_string(128), nullable=False), Column('version', Integer, nullable=False, default=1),
    Column('role', String(20), nullable=False), Column('origin', String(20), nullable=False),
    Column('sender_id', identity_string(80), nullable=False), Column('sender_name', String(120), nullable=False, default=''),
    Column('avatar', String(1024), nullable=False, default=''), Column('item_id', identity_string(64), nullable=False, default=''),
    Column('content', Text, nullable=False), Column('content_type', String(20), nullable=False, default='text'),
    Column('status', String(20), nullable=False, default='confirmed'), Column('occurred_at', Double, nullable=False),
    UniqueConstraint('account_id', 'chat_id', 'event_id', name='uq_reply_event_identity'),
    Index('ix_reply_history', 'account_id', 'chat_id', 'cursor'))

reply_outbox = Table('xy_reply_outbox', metadata,
    Column('id', String(64), primary_key=True), Column('account_id', identity_string(80), nullable=False),
    Column('chat_id', identity_string(128), nullable=False), Column('request_id', identity_string(128), nullable=False),
    Column('content_hash', String(64), nullable=False), Column('status', String(20), nullable=False),
    Column('result', JSON, nullable=False), Column('version', Integer, nullable=False, default=1))

reply_once_slots = Table('xy_reply_once_slots', metadata,
    Column('id', String(64), primary_key=True), Column('account_id', identity_string(80), nullable=False),
    Column('chat_id', identity_string(128), nullable=False), Column('scope', identity_string(64), nullable=False),
    Column('request_id', identity_string(128), nullable=False), Column('status', String(20), nullable=False))

reply_pauses = Table('xy_reply_pauses', metadata,
    Column('id', String(64), primary_key=True), Column('account_id', identity_string(80), nullable=False),
    Column('chat_id', identity_string(128), nullable=False), Column('until', Double, nullable=False),
    Column('version', Integer, nullable=False, default=1))

reply_policies = Table('xy_reply_policies', metadata,
    Column('account_id', identity_string(80), primary_key=True), Column('strategy', String(20), nullable=False),
    Column('version', Integer, nullable=False, default=1),
    Column('block_personal', Boolean, nullable=False, default=True),
    Column('block_platform', Boolean, nullable=False, default=False))

exclusive_replies = Table('xy_exclusive_replies', metadata,
    Column('id', String(64), primary_key=True), Column('account_id', identity_string(80), nullable=False),
    Column('item_id', identity_string(64), nullable=False), Column('content', Text, nullable=False),
    Column('image_url', String(1024), nullable=False, default=''), Column('enabled', Boolean, nullable=False, default=True),
    Column('version', Integer, nullable=False, default=1),
    UniqueConstraint('account_id', 'item_id', name='uq_exclusive_account_item'))

advanced_filters = Table('xy_reply_filters', metadata,
    Column('owner_id', BigInteger, nullable=True, index=True),
    Column('pause_minutes', Integer, nullable=True),
    Column('id', String(64), primary_key=True), Column('account_id', identity_string(80), nullable=False, index=True),
    Column('pattern', String(256), nullable=False), Column('match_mode', String(20), nullable=False),
    Column('source', String(20), nullable=False), Column('item_id', identity_string(64), nullable=False, default=''),
    Column('actions', JSON, nullable=False), Column('enabled', Boolean, nullable=False, default=True),
    Column('version', Integer, nullable=False, default=1))

TABLES = [reply_events, reply_outbox, reply_once_slots, reply_pauses, reply_policies, exclusive_replies, advanced_filters]

reply_images = Table('xy_reply_images', metadata,
    Column('id', String(64), primary_key=True), Column('owner_id', BigInteger, nullable=False),
    Column('sha256', String(64), nullable=False), Column('path', String(1024), nullable=False),
    Column('url', String(1024), nullable=False), Column('mime', String(32), nullable=False),
    Column('size', Integer, nullable=False), Column('width', Integer, nullable=False), Column('height', Integer, nullable=False))
reply_image_refs = Table('xy_reply_image_refs', metadata,
    Column('id', String(64), primary_key=True), Column('image_id', String(64), nullable=False, index=True),
    Column('owner_id', BigInteger, nullable=False), Column('source', String(32), nullable=False), Column('source_id', String(128), nullable=False))
TABLES.extend([reply_images, reply_image_refs])

# 每账号提交栅栏：先锁此行再分配事件游标，持锁到事务提交。
reply_stream_heads = Table('xy_reply_stream_heads', metadata,
    Column('account_id', identity_string(80), primary_key=True),
    Column('version', BigInteger, nullable=False, default=0))
TABLES.append(reply_stream_heads)


def upgrade_reply_schema(connection, *, events_table=reply_events):
    """仅由正式迁移入口调用；兼容首轮旧表已创建但缺message_id的库。"""
    from sqlalchemy import inspect, text
    columns = {column['name'] for column in inspect(connection).get_columns(events_table.name)}
    table = connection.dialect.identifier_preparer.quote(events_table.name)
    if 'message_id' not in columns:
        connection.execute(text(f'ALTER TABLE {table} ADD COLUMN message_id VARCHAR(128) NULL'))
        connection.execute(text(f'UPDATE {table} SET message_id = event_id WHERE message_id IS NULL'))
        if connection.dialect.name == 'mysql':
            connection.execute(text(f'ALTER TABLE {table} MODIFY COLUMN message_id VARCHAR(128) NOT NULL'))

    upgrade_identity_columns(connection, [events_table, *(t for t in TABLES if t is not reply_events)])

    schema=inspect(connection)
    if schema.has_table(advanced_filters.name):
        columns={column['name'] for column in schema.get_columns(advanced_filters.name)}
        for name,definition in [('owner_id','BIGINT NULL'),('pause_minutes','INTEGER NULL')]:
            if name not in columns:
                connection.execute(text(f'ALTER TABLE xy_reply_filters ADD COLUMN {name} {definition}'))
        for index in advanced_filters.indexes:
            index.create(connection, checkfirst=True)
