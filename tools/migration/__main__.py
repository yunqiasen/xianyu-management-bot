"""Explicit S3 CLI. Target URL/key are read from named environment variables."""
import argparse
import json
import os
import re
import asyncio
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from .snapshot import Snapshot, MigrationError
from .mapping import plan
from .runner import Migrator, migration_metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description='脱敏 SQLite 快照迁移演练；账号始终停用')
    parser.add_argument('command', choices=('inspect', 'apply', 'verify', 'rollback-export', 'rollback-import', 'legacy-stop'))
    parser.add_argument('--source')
    parser.add_argument('--namespace')
    parser.add_argument('--key-env', default='XYMB_MIGRATION_KEY_HEX')
    parser.add_argument('--target-env', default='XYMB_MIGRATION_TARGET_URL')
    parser.add_argument('--archive-dir')
    parser.add_argument('--account')
    parser.add_argument('--quarantine', action='store_true')
    parser.add_argument('--incremental', action='store_true')
    parser.add_argument('--init-journal', action='store_true')
    source_key = parser.add_mutually_exclusive_group()
    source_key.add_argument('--source-key-env')
    source_key.add_argument('--source-key-file')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--dryrun', action='store_true')
    parser.add_argument('--runtime-state')
    parser.add_argument('--asset-manifest')
    parser.add_argument('--asset-source-root')
    parser.add_argument('--asset-target-root')
    parser.add_argument('--bundle')
    parser.add_argument('--rollback-stage')
    parser.add_argument('--legacy-url')
    parser.add_argument('--legacy-token-env', default='XYMB_LEGACY_CONTROL_TOKEN')
    parser.add_argument('--legacy-boundary')
    args = parser.parse_args(argv)
    engine = None
    try:
        if args.command == 'legacy-stop':
            from .control import LegacyControl
            if not args.legacy_url or not args.account:
                raise MigrationError('legacy_url_and_account_required')
            if args.execute and not os.environ.get(args.legacy_token_env):
                raise MigrationError('legacy_token_env_required')
            async def boundary_reader():
                return json.loads(Path(args.legacy_boundary).read_text())
            control=LegacyControl(args.legacy_url,args.account,token=os.environ.get(args.legacy_token_env,''),
                execute=args.execute,boundary_reader=boundary_reader if args.legacy_boundary else None)
            report=asyncio.run(control.quiesce())
            from .snapshot import digest
            public={k:v for k,v in report.items() if k not in ('inflight','watermark','account_id')}
            if isinstance(report.get('inflight'),list): public['inflight_count']=len(report['inflight'])
            if report.get('watermark') is not None: public['watermark_present']=True
            print(json.dumps(public,ensure_ascii=False))
            return 0 if report.get('stopped') or report.get('dryrun') else 2
        try:
            key = bytes.fromhex(os.environ[args.key_env])
        except (KeyError, ValueError):
            raise MigrationError('migration_key_env_required') from None
        if args.command == 'rollback-import':
            from .rollback import RollbackImporter
            if not all((args.archive_dir,args.bundle,args.rollback_stage,args.account,args.namespace)):
                raise MigrationError('rollback_arguments_required')
            runner=Migrator(None,key=key,archive_dir=args.archive_dir)
            report=RollbackImporter(runner,args.rollback_stage,account_id=args.account,namespace=args.namespace).apply(args.bundle,execute=args.execute)
            print(json.dumps(report,ensure_ascii=False))
            return 2 if report['conflicts'] else 0
        if not args.source or not args.namespace:
            raise MigrationError('source_and_namespace_required')
        from .legacy import load_source_key
        prepared = plan(Snapshot.read(args.source), namespace=args.namespace, key=key,
            source_key=load_source_key(env=args.source_key_env,key_file=args.source_key_file),
            runtime_state=json.loads(Path(args.runtime_state).read_text()) if args.runtime_state else None)
        if args.asset_manifest:
            if not all((args.archive_dir,args.asset_source_root,args.asset_target_root)):
                raise MigrationError('asset_arguments_required')
            prepared=Migrator(None,key=key,archive_dir=args.archive_dir).relocate_attachments(prepared,
                json.loads(Path(args.asset_manifest).read_text()),source_root=args.asset_source_root,target_root=args.asset_target_root)
        if args.command == 'inspect':
            report = prepared.report()
            print(json.dumps(report, ensure_ascii=False))
            return 2 if report['blocked'] else 0
        if not args.archive_dir:
            raise MigrationError('archive_dir_required')
        if args.command in ('verify', 'rollback-export') and not args.account:
            raise MigrationError('account_required')
        try:
            url = make_url(os.environ[args.target_env])
        except (KeyError, ValueError):
            raise MigrationError('target_url_env_required') from None
        # The shipped CLI is rehearsal-only, never points at configured application DBs.
        if (url.get_backend_name() != 'mysql' or url.host not in ('localhost', '127.0.0.1', '::1')
                or not re.fullmatch(r'xymb_migration_[a-z0-9_]+', url.database or '')):
            raise MigrationError('isolated_mysql_target_required')
        engine = create_engine(url, hide_parameters=True, echo=False, pool_pre_ping=True)
        if args.init_journal:
            migration_metadata.create_all(engine)
        runner = Migrator(engine, key=key, archive_dir=args.archive_dir)
        if args.command == 'apply':
            report = runner.apply(prepared, quarantine=args.quarantine, incremental=args.incremental)
        elif args.command == 'verify':
            report = runner.verify(prepared, account_id=args.account)
        else:
            report = runner.export_rollback(prepared, account_id=args.account)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except MigrationError as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({'error': 'migration_input_or_target_error'}))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
