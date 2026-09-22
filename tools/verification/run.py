#!/usr/bin/env python3
"""Collect and run isolated multi-service regressions; never a release approval."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import secrets
from dataclasses import asdict, dataclass
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Suite:
    name: str
    command: tuple[str, ...]
    pythonpath: str = 'backend-web:.'
    kind: str = 'unittest'


def collect_suites(root: Path = ROOT) -> list[Suite]:
    python = str(root / '.venv/bin/python')
    suites = []
    for domain in sorted((root / 'tests').iterdir()):
        if domain.is_dir() and list(domain.glob('test_*.py')) and domain.name != 'monitor':
            suites.append(Suite(domain.name, (python, '-m', 'unittest', 'discover', '-s',
                f'tests/{domain.name}', '-v'), 'websocket:.' if domain.name == 'dispatch' else 'backend-web:.'))
    suites.append(Suite('monitor', (python, '-m', 'pytest', 'tests/monitor', '--asyncio-mode=auto', '-q'), kind='pytest'))
    for domain in ('replies', 'runtime'):
        for path in sorted((root / 'tests' / domain).glob('*suite.py')):
            suites.append(Suite(f'{domain}-{path.stem}', (python, str(path.relative_to(root)), '-v')))
    for path in sorted((root / 'tests/commerce').glob('ws_*.py')):
        # ws_scheduler deliberately loads the scheduler by file; its app imports are websocket.
        suites.append(Suite(f'commerce-{path.stem}', (python, str(path.relative_to(root)), '-v'), 'websocket:.'))
    suites.extend([
        Suite('dispatch-mysql', (python, 'tests/dispatch/integration_check.py'), 'websocket:.'),
        Suite('upstream', (python, '-m', 'unittest', 'discover', '-s', 'common/tests', '-v'), 'websocket:.'),
        Suite('frontend-lint', ('npm', '--prefix', 'frontend', 'run', 'lint'), kind='static'),
        Suite('frontend-build', ('npm', '--prefix', 'frontend', 'run', 'build'), kind='build'),
    ])
    for name, file in [
        ('ai-ui', 'ai/test_frontend.mjs'), ('bargaining-ui', 'bargaining/test_frontend.mjs'),
        ('reply-ui', 'replies/frontend.cjs'), ('products-ui', 'products/ui-flow.cjs'),
        ('renewal-ui', 'accounts/test_renewal_ui.cjs'), ('runtime-ui', 'runtime/test_ai_route.mjs'),
    ]:
        suites.append(Suite(name, ('node', 'tests/' + file), kind='frontend'))
    for domain, file in [('accounts', 'browser_policy.py'), ('ai', 'browser_smoke.py'), ('bargaining', 'browser_smoke.py'), ('products', 'browser_flow.py'), ('admin', 'browser_flow.py'), ('commerce', 'browser_flow.py'), ('replies', 'browser_flow.py')]:
        suites.append(Suite(f'{domain}-browser', (python, f'tests/{domain}/{file}'), kind='browser'))
    return suites


def run_suite(suite: Suite, root: Path, logfile: Path, environment: dict, timeout: float) -> dict:
    """Run one independent process; skips and empty test discovery fail the gate."""
    started = time.monotonic()
    artifacts = logfile.parent / (suite.name + '-artifacts')
    artifacts.mkdir(mode=0o700, exist_ok=True)
    env = {**os.environ, **environment, 'PYTHONPATH': suite.pythonpath, 'SQL_ECHO': 'false',
           'XYMB_VERIFY_OUTPUT': str(artifacts.resolve())}
    timed_out = False
    with logfile.open('w') as log:
        process = subprocess.Popen(suite.command, cwd=root, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            code = 124
    text = logfile.read_text(errors='replace')
    if suite.kind == 'unittest':
        counts = re.findall(r'^Ran (\d+) tests? in ', text, re.MULTILINE)
        tests = sum(map(int, counts))
        skipped = sum(map(int, re.findall(r'\bskipped=(\d+)', text)))
    elif suite.kind == 'pytest':
        counts = re.findall(r'\b(\d+) passed\b', text)
        tests = int(counts[-1]) if counts else 0
        skips = re.findall(r'\b(\d+) skipped\b', text)
        skipped = int(skips[-1]) if skips else 0
    else:
        tests, skipped = None, 0
    return {**asdict(suite), 'exit': code, 'tests': tests, 'skipped': skipped,
            'passed': code == 0 and skipped == 0 and tests != 0,
            'timed_out': timed_out, 'seconds': round(time.monotonic() - started, 3),
            'log': logfile.name}


def validate_integration_environment(path: Path) -> None:
    values = dict(line.split('=', 1) for line in path.read_text().splitlines()
                  if '=' in line and not line.startswith('#'))
    if tuple(values.get(key) for key in ('MYSQL_HOST', 'MYSQL_PORT', 'MYSQL_DATABASE')) != (
            '127.0.0.1', '19006', 'xymb_integration'):
        raise ValueError('isolated_environment_required')


@contextmanager
def commerce_fixture():
    """Create/drop only a newly named schema in the explicitly isolated container."""
    from sqlalchemy.engine import URL
    container = 'xymb-integration-mysql-1'
    label = subprocess.check_output(['docker', 'inspect', '--format',
        '{{ index .Config.Labels "com.docker.compose.project" }}', container], text=True).strip()
    if label != 'xymb-integration':
        raise ValueError('isolated_container_required')
    suffix = secrets.token_hex(8)
    database, user, password = 'xy_commerce_fixture_' + suffix, 'verify_' + suffix, secrets.token_hex(24)

    def admin(sql):
        result = subprocess.run(['docker', 'exec', '-i', container, 'sh', '-c',
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=socket -uroot --batch'],
            input=sql, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError('isolated_fixture_admin_failed')

    try:
        admin(f"CREATE DATABASE `{database}`; CREATE USER '{user}'@'%' IDENTIFIED BY '{password}'; "
              f"GRANT ALL ON `{database}`.* TO '{user}'@'%';")
        yield URL.create('mysql+asyncmy', username=user, password=password,
            host='127.0.0.1', port=19006, database=database).render_as_string(hide_password=False)
    finally:
        admin(f"DROP DATABASE IF EXISTS `{database}`; DROP USER IF EXISTS '{user}'@'%';")


def source_fingerprint(root: Path) -> str:
    paths = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=root)
    digest = hashlib.sha256()
    for name in sorted(set(paths.split(b'\0')) - {b''}):
        path = root / os.fsdecode(name)
        digest.update(name + b'\0')
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b'deleted')
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help='print commands without running them')
    parser.add_argument('--suite', action='append', default=[], help='run named suites only; not full acceptance')
    parser.add_argument('--output', type=Path, help='new evidence directory outside the source repository')
    parser.add_argument('--integration-env', type=Path, help='private environment for isolated dependencies')
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    suites = collect_suites()
    if args.suite:
        names = {suite.name for suite in suites}
        if set(args.suite) - names:
            parser.error('unknown_suite')
        suites = [suite for suite in suites if suite.name in args.suite]
    if args.plan:
        print(json.dumps([asdict(suite) for suite in suites], indent=2))
        return 0
    if args.output is None or args.integration_env is None or args.timeout <= 0:
        parser.error('--output, --integration-env and positive --timeout required')
    output, private_env = args.output.resolve(), args.integration_env.resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error('evidence_directory_must_be_outside_source')
    validate_integration_environment(private_env)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    start_hash = source_fingerprint(ROOT)
    report = {'started_at': datetime.now(timezone.utc).isoformat(),
              'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'source_fingerprint': start_hash, 'full_run': not args.suite,
              'synthetic': True, 'release_approved': False, 'results': []}
    environment = {'XYMB_INTEGRATION_ENV': str(private_env)}
    try:
        with commerce_fixture() as commerce_url:
            environment['XYMB_COMMERCE_MYSQL_URL'] = commerce_url
            # Serial execution prevents shared browser fixtures and DB upgrades racing each other.
            for suite in suites:
                result = run_suite(suite, ROOT, output / (suite.name + '.log'), environment, args.timeout)
                report['results'].append(result)
                print(f"{suite.name}: {'PASS' if result['passed'] else 'FAIL'} "
                      f"tests={result['tests']} skipped={result['skipped']}", flush=True)
    except Exception as exc:
        report['error'] = type(exc).__name__  # never expose environment, SQL or credentials
    report['source_unchanged'] = start_hash == source_fingerprint(ROOT)
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    report['passed'] = (not report.get('error') and report['source_unchanged']
                        and len(report['results']) == len(suites)
                        and all(result['passed'] for result in report['results']))
    report['tests'] = sum(result['tests'] or 0 for result in report['results'])
    report['skipped'] = sum(result['skipped'] for result in report['results'])
    (output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {report['tests']} tests; "
          f"{report['skipped']} skipped; {len(report['results'])}/{len(suites)} suites")
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
