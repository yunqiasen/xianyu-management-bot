"""S3 rehearsal coordinator; the old-process quiesce adapter is a release boundary.

Acquiring a lease never enables an account. No platform transport is imported.
The source application must independently implement stop_old's durable boundary.
"""
from __future__ import annotations

import fcntl
import os
from functools import wraps

from .runner import Migrator
from .snapshot import MigrationError, digest


def single_coordinator(method):
    @wraps(method)
    async def guarded(self, *args, **kwargs):
        fd = os.open(self.store.root / (self.name + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MigrationError('handoff_coordinator_busy') from None
            self.state = self.read_state()
            return await method(self, *args, **kwargs)
        finally:
            os.close(fd)
    return guarded


class Handoff:
    def __init__(self, account_id, old_lease, new_lease, *, key, archive_dir):
        expected_key = 'xymb:account:{' + str(account_id).encode().hex() + '}:lease'
        if old_lease.key != expected_key or new_lease.key != expected_key or old_lease is new_lease:
            raise MigrationError('handoff_account_mismatch')
        self.account_id, self.old, self.new = account_id, old_lease, new_lease
        self.store = Migrator(None, key=key, archive_dir=archive_dir)
        self.name = 'handoff-' + digest(account_id, key) + '.enc'
        self.state = self.read_state()

    def read_state(self):
        if (self.store.root / self.name).exists():
            return self.store.read_bundle(self.name)
        return {'state': 'initial'}

    def _save(self, state, **values):
        self.state = {**self.state, **values, 'state': state}
        self.store.write_bundle(self.name, self.state)
        return dict(self.state)

    @single_coordinator
    async def switch(self, *, stop_old, final_import):
        if self.state['state'] != 'initial':
            # A restart does not infer the status of an old process or replay a switch.
            return {**self.state, 'restart_reconciliation_required': True}
        self._save('stopping_old')
        try:
            await self.old.check()
            boundary = await stop_old()
            if boundary.get('stopped') is not True or boundary.get('watermark') is None:
                return self._save('old_stop_unconfirmed')
            # Old application has stopped admitting writes, then its token is revoked.
            if not await self.old.release():
                return self._save('old_release_unconfirmed')
            inflight = boundary.get('inflight')
            if not isinstance(inflight, list):
                return self._save('old_boundary_incomplete')
            self._save('old_stopped', watermark_hash=digest(boundary['watermark'], self.store.key),
                       old_generation=self.old.generation, inflight_count=len(inflight))
            if inflight:
                return self._save('paused_inflight')
            result = await final_import()
            if (result.get('verified') is not True or result.get('blocked') is not False
                    or result.get('account_disabled') is not True
                    or not isinstance(result.get('checkpoint'), int)
                    or len(result.get('checksum', '')) != 64):
                return self._save('paused_import_unverified')
            self._save('final_snapshot_imported', checksum=result['checksum'], checkpoint=result['checkpoint'])
            if not await self.new.acquire():
                return self._save('paused_new_lease_busy')
            await self.new.check()
            return self._save('lease_acquired_account_disabled', new_generation=self.new.generation)
        except Exception:
            await self.new.release()
            return self._save('paused_import_error')

    @single_coordinator
    async def rollback(self, *, stop_new, export_delta):
        self._save('stopping_new_for_rollback')
        try:
            boundary = await stop_new()
            if boundary.get('stopped') is not True or boundary.get('watermark') is None:
                return self._save('new_stop_unconfirmed')
            if not await self.new.release():
                return self._save('new_release_unconfirmed')
            # Unknown results are exported, not resent; old execution stays revoked.
            exported = await export_delta()
            return self._save('paused_reconciliation_required', delta_bundle=exported['bundle'],
                              delta_checksum=exported['checksum'], rollback_inflight_count=len(boundary.get('inflight', [])))
        except Exception:
            return self._save('paused_export_error')
