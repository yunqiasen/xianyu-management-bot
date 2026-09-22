"""Content-only proof for destructive maintenance; no source values enter reports."""
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError('unsupported_snapshot_value')


def data_fingerprint(rows):
    hashes = sorted(sha256(json.dumps(dict(row), sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), default=_json_value).encode()).hexdigest() for row in rows)
    return sha256('\n'.join(hashes).encode()).hexdigest()
