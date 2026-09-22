"""Transactional SQLAlchemy importer. Only migration bookkeeping is defined here."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile
import time

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (MetaData, Table, Column, String, Integer, select, insert, update,
                        and_, or_, inspect, text)
from sqlalchemy.exc import SQLAlchemyError

from .snapshot import MigrationError, canonical, digest
from .mapping import model_tables

migration_metadata = MetaData()
checkpoints = Table('xymb_migration_checkpoints', migration_metadata,
    Column('namespace', String(40), primary_key=True), Column('identity', String(64), primary_key=True),
    Column('target', String(80), nullable=False), Column('source_hash', String(64), nullable=False),
    Column('target_hash', String(64), nullable=False))
batches = Table('xymb_migration_batches', migration_metadata,
    Column('namespace', String(40), primary_key=True), Column('key_check', String(64), nullable=False),
    Column('checksum', String(64), nullable=False), Column('state', String(40), nullable=False),
    Column('checkpoint', Integer, nullable=False, default=0))


class Migrator:
    def __init__(self, engine, *, key: bytes, archive_dir):
        self.engine, self.key = engine, key
        if engine is not None:
            engine.hide_parameters = True
        self.key_check = digest('migration-key-check-v1', key)
        self.fernet = Fernet(base64.urlsafe_b64encode(bytes.fromhex(digest('migration-encryption-v1', key))))
        self.root = Path(archive_dir)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise MigrationError('archive_symlink')

    def write_bundle(self, name, payload):
        if not re.fullmatch(r'[a-z0-9_-]+\.enc', name):
            raise MigrationError('invalid_bundle_name')
        fd, temp = tempfile.mkstemp(dir=self.root, prefix='.writing-')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(self.fernet.encrypt(canonical(payload).encode()))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.root / name)
            directory = os.open(self.root, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return name

    def read_bundle(self, name):
        if not re.fullmatch(r'[a-z0-9_-]+\.enc', name) or (self.root / name).is_symlink():
            raise MigrationError('invalid_bundle_name')
        try:
            return json.loads(self.fernet.decrypt((self.root / name).read_bytes()))
        except (InvalidToken, OSError, ValueError):
            raise MigrationError('archive_integrity_failed') from None

    def read_archive(self, checksum):
        return self.read_bundle('snapshot-' + checksum + '.enc')

    def _archive(self, plan):
        if plan.runtime_state is not None:
            self.write_bundle('runtime-'+digest(plan.runtime_state,self.key)+'.enc',plan.runtime_state)
        name = 'snapshot-' + plan.checksum + '.enc'
        payload = dict(tables=plan.snapshot.tables, schema=plan.snapshot.schema,
                       identities=plan.snapshot.identities, checksum=plan.checksum)
        if (self.root / name).exists():
            if canonical(self.read_bundle(name)) != canonical(payload):
                raise MigrationError('archive_content_conflict')
        else:
            self.write_bundle(name, payload)

    @contextmanager
    def _connection(self):
        # MySQL named lock spans row commits, serializing all migration namespaces.
        with self.engine.connect() as conn:
            mysql = conn.dialect.name == 'mysql'
            if mysql:
                if conn.scalar(text("SELECT GET_LOCK('xymb_migration_import_v1', 0)")) != 1:
                    raise MigrationError('migration_busy')
                conn.commit()
            try:
                yield conn
            finally:
                if conn.in_transaction():
                    conn.rollback()
                if mysql:
                    conn.execute(text("SELECT RELEASE_LOCK('xymb_migration_import_v1')"))
                    conn.commit()

    def _begin(self, conn):
        if conn.dialect.name == 'sqlite':
            conn.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            conn.begin()

    def _find(self, conn, step):
        table = model_tables()[step.target]
        clauses = [and_(*(table.c[k] == step.values[k] for k in step.lookup))]
        pk_names = [c.name for c in table.primary_key]
        if all(k in step.values for k in pk_names):
            clauses.append(and_(*(table.c[k] == step.values[k] for k in pk_names)))
        # Include all target unique constraints (e.g. user's email, not just username).
        for col in table.c:
            if col.unique and col.name in step.values and step.values[col.name] is not None:
                clauses.append(col == step.values[col.name])
        found = conn.execute(select(table).where(or_(*clauses)).with_for_update()).mappings().all()
        if len(found) > 1:
            raise MigrationError('target_identity_conflict')
        return dict(found[0]) if found else None

    def _check(self, conn, plan, step, incremental):
        journal = conn.execute(select(checkpoints).where(checkpoints.c.namespace == plan.namespace,
                  checkpoints.c.identity == step.identity).with_for_update()).mappings().one_or_none()
        current = self._find(conn, step)
        incoming = digest({'source': step.source_hash, 'values': step.values}, self.key)
        if journal:
            if journal['target'] != step.target or current is None or digest(current, self.key) != journal['target_hash']:
                raise MigrationError('target_changed')
            if journal['source_hash'] != incoming and not incremental:
                raise MigrationError('incremental_required')
        elif current is not None:
            raise MigrationError('target_identity_conflict')
        return journal, current, incoming

    def apply(self, plan, *, quarantine=False, incremental=False, interrupt_after=None):
        if plan.report()['blocked'] and not quarantine:
            raise MigrationError('mapping_blocked')
        if plan.snapshot.checksum(self.key) != plan.checksum:
            raise MigrationError('plan_integrity_failed')
        self._archive(plan)
        result = {'inserted': 0, 'updated': 0, 'unchanged': 0, 'state': 'quarantined', 'checksum': plan.checksum}
        try:
            with self._connection() as conn:
                self._begin(conn)
                batch = conn.execute(select(batches).where(batches.c.namespace == plan.namespace).with_for_update()).mappings().one_or_none()
                if batch and batch['key_check'] != self.key_check:
                    raise MigrationError('migration_key_changed')
                if not batch:
                    conn.execute(insert(batches).values(namespace=plan.namespace, key_check=self.key_check,
                        checksum=plan.checksum, state='importing', checkpoint=0))
                # Preflight entire batch: a late conflict should not leave early rows updated.
                identities, primary_keys = set(), set()
                for step in plan.steps:
                    target_identity = (step.target, canonical([step.values[k] for k in step.lookup]))
                    if target_identity in identities:
                        raise MigrationError('duplicate_target_identity')
                    identities.add(target_identity)
                    columns = model_tables()[step.target].primary_key.columns.keys()
                    if all(k in step.values for k in columns):
                        pk = (step.target, canonical([step.values[k] for k in columns]))
                        if pk in primary_keys:
                            raise MigrationError('duplicate_target_primary_key')
                        primary_keys.add(pk)
                    self._check(conn, plan, step, incremental)
                conn.commit()
                for index, step in enumerate(plan.steps):
                    if interrupt_after == index:
                        raise MigrationError('injected_interrupt')
                    self._begin(conn)
                    journal, current, incoming = self._check(conn, plan, step, incremental)
                    table = model_tables()[step.target]
                    if journal and incoming == journal['source_hash']:
                        result['unchanged'] += 1
                    else:
                        if journal:
                            predicate = and_(*(c == current[c.name] for c in table.primary_key))
                            conn.execute(update(table).where(predicate).values(**step.values))
                            result['updated'] += 1
                        else:
                            conn.execute(insert(table).values(**step.values))
                            result['inserted'] += 1
                        persisted = self._find(conn, step)
                        checkpoint = dict(source_hash=incoming, target_hash=digest(persisted, self.key))
                        if journal:
                            conn.execute(update(checkpoints).where(checkpoints.c.namespace == plan.namespace,
                                checkpoints.c.identity == step.identity).values(**checkpoint))
                        else:
                            conn.execute(insert(checkpoints).values(namespace=plan.namespace, identity=step.identity,
                                target=step.target, **checkpoint))
                    conn.execute(update(batches).where(batches.c.namespace == plan.namespace).values(checkpoint=index + 1, state='importing'))
                    conn.commit()
                self._begin(conn)
                conn.execute(update(batches).where(batches.c.namespace == plan.namespace).values(checksum=plan.checksum, state='quarantined'))
                conn.commit()
        except SQLAlchemyError:
            # SQLAlchemy repr contains parameters, including credentials and card contents.
            raise MigrationError('target_database_error') from None
        return result

    @staticmethod
    def _scoped_source(tables, account_id):
        selected=next(r for r in tables['cookies'] if r['id']==account_id)
        owner=selected['user_id']
        output={}
        for name,rows in tables.items():
            if name=='users': filtered=[r for r in rows if r['id']==owner]
            elif name=='cookies': filtered=[selected]
            elif name=='system_settings': filtered=[]
            elif name=='notification_templates': filtered=[r for r in rows if r.get('user_id') in (None,owner)]
            else:
                filtered=[]
                for row in rows:
                    if 'cookie_id' in row:
                        keep=row['cookie_id']==account_id or (row['cookie_id'] in (None,'') and row.get('user_id')==owner)
                    elif 'account_id' in row: keep=row['account_id']==account_id
                    elif 'user_id' in row: keep=row['user_id']==owner
                    else: keep=False
                    if keep: filtered.append(row)
            output[name]=filtered
        return output

    def export_rollback(self, plan, *, account_id):
        """Pause only this account, export present facts; never restore/delete old rows."""
        tables = model_tables()
        accounts = tables['xy_accounts']
        try:
            with self._connection() as conn:
                self._begin(conn)
                row = conn.execute(select(accounts).where(accounts.c.account_id == account_id).with_for_update()).mappings().one_or_none()
                if row is None:
                    raise MigrationError('missing_target_account')
                if (row.get('metadata') or {}).get('migration', {}).get('namespace') != plan.namespace:
                    raise MigrationError('account_namespace_mismatch')
                conn.execute(update(accounts).where(accounts.c.id == row['id']).values(status='disabled', disable_reason='migration_rollback_reconcile'))
                # Commit pause first; export failure must leave execution stopped.
                conn.commit()
                self._begin(conn)
                present = set(inspect(conn).get_table_names())
                payload, shared, unscoped = {}, [], []
                for name, table in tables.items():
                    if name not in present:
                        continue
                    predicate = None
                    for column in ('cookie_id', 'account_identifier', 'account_pk', 'account_id'):
                        if column in table.c:
                            value = row['id'] if isinstance(table.c[column].type, Integer) else account_id
                            predicate = table.c[column] == value
                            owner_column=next((field for field in ('owner_id','user_id') if field in table.c),None)
                            if owner_column and not isinstance(table.c[column].type,Integer):
                                predicate=or_(predicate,and_(table.c[owner_column]==row['owner_id'],
                                    or_(table.c[column].is_(None),table.c[column]=='')))
                                shared.append(name)
                            break
                    if name == 'xy_users':
                        predicate = table.c.id == row['owner_id']
                    if predicate is None:
                        for column in ('owner_id', 'user_id'):
                            if column in table.c:
                                predicate = table.c[column] == row['owner_id']
                                shared.append(name)
                                break
                    if name=='xy_notify_templates':
                        predicate=or_(table.c.owner_id==row['owner_id'],table.c.owner_id==0)
                    if predicate is not None:
                        payload[name] = [dict(r) for r in conn.execute(select(table).where(predicate)).mappings()]
                    else:
                        unscoped.append(name)
                conn.commit()
        except SQLAlchemyError:
            raise MigrationError('target_database_error') from None
        checksum = digest(payload, self.key)
        exported_at_ns=time.time_ns()
        bundle = self.write_bundle('rollback-' + digest([checksum,plan.checksum,exported_at_ns],self.key) + '.enc', {'account': account_id, 'tables': payload,
                    'source_checksum': plan.checksum, 'namespace': plan.namespace, 'exported_at_ns': exported_at_ns,
                    'source_tables': self._scoped_source(plan.snapshot.tables, account_id), 'shared_owner_tables': shared, 'unscoped_tables': unscoped})
        return {'state': 'paused_reconciliation_required', 'bundle': bundle, 'checksum': checksum,
                'counts': {name: len(rows) for name, rows in payload.items()},
                'shared_owner_tables': shared, 'unscoped_tables': unscoped}

    def verify(self, plan, *, account_id):
        """Read target rows and checkpoints, not the planner's expected counts alone."""
        from sqlalchemy import func
        accounts = model_tables()['xy_accounts']
        try:
            with self._connection() as conn:
                self._begin(conn)
                batch = conn.execute(select(batches).where(batches.c.namespace == plan.namespace)).mappings().one_or_none()
                if not batch or batch['checksum'] != plan.checksum or batch['state'] != 'quarantined':
                    raise MigrationError('batch_not_complete')
                for step in plan.steps:
                    journal, current, incoming = self._check(conn, plan, step, False)
                    if journal is None:
                        raise MigrationError('checkpoint_missing')
                total = conn.scalar(select(func.count()).select_from(checkpoints).where(checkpoints.c.namespace == plan.namespace))
                account = conn.execute(select(accounts).where(accounts.c.account_id == account_id)).mappings().one_or_none()
                if not account or (account.get('metadata') or {}).get('migration', {}).get('namespace') != plan.namespace:
                    raise MigrationError('missing_target_account')
                conn.commit()
        except SQLAlchemyError:
            raise MigrationError('target_database_error') from None
        removed = total - len(plan.steps)
        return {'verified': True, 'checksum': plan.checksum, 'checkpoint': len(plan.steps),
                'account_disabled': account['status'] == 'disabled', 'removed_source_rows': removed,
                'blocked': bool(plan.report()['blocked'] or removed),
                'mapped_counts': plan.report()['mapped_counts']}

    def relocate_attachments(self, prepared, manifest, *, source_root, target_root):
        from .assets import relocate
        return relocate(self, prepared, manifest, source_root=source_root, target_root=target_root)

    def archive_attachments(self, references, *, root, max_bytes=100 * 1024 * 1024):
        """Archive explicitly selected local assets; remote URLs are never fetched.

        This does not relocate files into the web upload tree. That owner-scoped
        restoration remains a separate integration gate.
        """
        import hashlib
        from urllib.parse import urlsplit, unquote
        root = Path(root).resolve()
        files, total = {}, 0
        for reference in dict.fromkeys(references):
            parsed = urlsplit(reference)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                raise MigrationError('attachment_requires_local_reference')
            relative = Path(unquote(parsed.path))
            path = (root / relative).resolve()
            if relative.is_absolute() or path == root or root not in path.parents:
                raise MigrationError('attachment_path_escape')
            if not path.is_file():
                raise MigrationError('attachment_not_found')
            if path.stat().st_size + total > max_bytes:
                raise MigrationError('attachment_size_limit')
            try:
                data = path.read_bytes()
            except OSError:
                raise MigrationError('attachment_read_error') from None
            total += len(data)
            if total > max_bytes:
                raise MigrationError('attachment_size_limit')
            files[reference] = {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
                                'base64': base64.b64encode(data).decode()}
        checksum = digest(files, self.key)
        bundle = self.write_bundle('attachments-' + checksum + '.enc', files)
        return {'bundle': bundle, 'checksum': checksum, 'verified_count': len(files), 'bytes': total,
                'restore_state': 'owner_scoped_asset_restore_required'}
