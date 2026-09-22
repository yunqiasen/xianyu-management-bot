"""Opaque event identifiers are byte-sensitive on the supported MySQL 8 backend."""
from sqlalchemy import MetaData, String, Table, inspect
from sqlalchemy.schema import CreateColumn

IDENTITY_COLLATION = 'utf8mb4_0900_bin'  # NO PAD: a trailing space remains part of an ID.


def identity_string(length):
    return String(length).with_variant(String(length, collation=IDENTITY_COLLATION), 'mysql')


def upgrade_identity_columns(connection, tables):
    """Upgrade existing fork columns in place, preserving lengths/defaults/nullability."""
    if connection.dialect.name != 'mysql':
        return
    schema = inspect(connection)
    quote = connection.dialect.identifier_preparer.quote
    for table in tables:
        if not schema.has_table(table.name):
            continue
        expected = [column.name for column in table.c
                    if getattr(column.type.dialect_impl(connection.dialect), 'collation', None)
                    == IDENTITY_COLLATION]
        if not expected:
            continue
        reflected = Table(table.name, MetaData(), autoload_with=connection)
        changes = []
        for name in expected:
            column = reflected.c[name]
            if column.type.collation != IDENTITY_COLLATION:
                column.type = String(column.type.length, collation=IDENTITY_COLLATION)
                changes.append('MODIFY COLUMN ' + str(CreateColumn(column).compile(dialect=connection.dialect)))
        if changes:
            connection.exec_driver_sql(f'ALTER TABLE {quote(table.name)} ' + ', '.join(changes))
