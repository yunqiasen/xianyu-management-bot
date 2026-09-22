"""Read-only consistent SQLite snapshot, with schema-aware source identities."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote


class MigrationError(Exception):
    """Public errors contain stable codes only, never SQL parameters or row values."""


def canonical(value):
    def encode(obj):
        if isinstance(obj, bytes):
            return {'$bytes': base64.b64encode(obj).decode('ascii')}
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return str(obj)
        raise TypeError(type(obj).__name__)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=encode)


def digest(value, key: bytes):
    if len(key) < 32:
        raise MigrationError('weak_integrity_key')
    return hmac.new(key, canonical(value).encode(), hashlib.sha256).hexdigest()


def ident(name):
    return '"' + name.replace('"', '""') + '"'


@dataclass(repr=False)
class Snapshot:
    tables: dict = field(repr=False)
    schema: dict
    identities: dict = field(repr=False)

    @classmethod
    def read(cls, path, *, max_rows=1_000_000):
        path = Path(path).resolve()
        if not path.is_file():
            raise MigrationError('source_not_file')
        source = frozen = None
        try:
            source = sqlite3.connect(f'file:{quote(str(path))}?mode=ro', uri=True)
            source.execute('PRAGMA query_only=ON')
            frozen = sqlite3.connect(':memory:')
            source.backup(frozen)
            frozen.row_factory = sqlite3.Row
            if frozen.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise MigrationError('source_integrity_failed')
            if frozen.execute('PRAGMA foreign_key_check').fetchone():
                raise MigrationError('source_foreign_key_failed')
            names = [r[0] for r in frozen.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            if not {'users', 'cookies'}.issubset(names):
                raise MigrationError('missing_required_table')
            tables, schema, identities = {}, {}, {}
            total = 0
            for name in names:
                columns = [dict(r) for r in frozen.execute(f'PRAGMA table_info({ident(name)})')]
                schema[name] = columns
                rows = [dict(r) for r in frozen.execute(f'SELECT * FROM {ident(name)} LIMIT ?', (max_rows + 1,))]
                total += len(rows)
                if total > max_rows:
                    raise MigrationError('source_row_limit_exceeded')
                pks = [c['name'] for c in sorted(columns, key=lambda c: c['pk']) if c['pk']]
                keys = []
                for i, row in enumerate(rows):
                    if name == 'keywords':
                        key = [row.get('cookie_id'), row.get('keyword'), row.get('item_id') or None]
                    elif pks:
                        key = [row[k] for k in pks]
                    else:
                        # Unsupported tables remain archived; ordinal is not used to import.
                        key = [i]
                    keys.append(canonical(key))
                if len(set(keys)) != len(keys):
                    raise MigrationError('duplicate_source_identity:' + name)
                tables[name], identities[name] = rows, keys
            return cls(tables, schema, identities)
        except sqlite3.Error:
            raise MigrationError('invalid_sqlite_snapshot') from None
        finally:
            if source is not None:
                source.close()
            if frozen is not None:
                frozen.close()

    def inventory(self):
        return {name: {'count': len(rows), 'columns': [c['name'] for c in self.schema[name]],
                       'primary_key': [c['name'] for c in self.schema[name] if c['pk']]}
                for name, rows in self.tables.items()}

    def checksum(self, key):
        # Row order of tables without a PK is meaningful only for their opaque archive.
        return digest({'schema': self.schema, 'tables': self.tables}, key)
