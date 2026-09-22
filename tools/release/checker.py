"""Read-only candidate evidence gate. No build, deploy, network or database access.

Receipts are human-reviewed attestations, not proof of truth by themselves.
Only hashed attachments, fixed scope and committed source are checked here.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

BASELINE = 'fdc8eb039be771456ecfbbbe43fa26f57feb3947'
BRANCH = 'xianyu-management-bot-fork'
INPUT_HASHES = {
    'spec': '305e4abcb515fcefc98def5456ee897859ee8059a2b83bd2751e1d384d62eac5',
    'matrix': '22cc6973604004e5f82c0883ca511747837e0c9e8f7ac0e35c3512c0d60584b0',
    'patches': 'feb92cbdb8ffbd0e0b7975196f6c35c233171a5ee78f3f43874c4f6620d0da4b',
}
P5 = {'AT21', 'AT22', 'AT23', 'AT24', 'DEV45', 'DEV46'}
REAL_SCENARIOS = (
    'messages', 'ai', 'publish_single', 'publish_batch', 'orders_inventory_confirmation',
    'notifications', 'recovery', 'handoff_single_executor', 'rollback_preserves_delta',
    'actual_deployed_version', 'retained_image_checkpoint', 'observation_window',
)
CHECK_KINDS = {'integration', 'browser', 'build', 'static', 'migration', 'restore', 'runtime'}
KINDS = CHECK_KINDS | {'document', 'review', 'approval'}
GATES = {
    'regression': ('integration', ('S1', 'S2', 'S3')),
    'frontend_build': ('build', ()),
    'candidate_build': ('build', ()),
    'frontend_operations': ('browser', ('S1',)),
    'static_checks': ('static', ()),
    'api_contracts': ('integration', ('S1',)),
    'mysql_redis': ('integration', ('S1', 'S2')),
    'migration': ('migration', ('S3',)),
    'restore': ('restore', ('S1', 'S3')),
    'rollback': ('migration', ('S1', 'S2', 'S3')),
    'documentation': ('document', ()),
    'sources': ('document', ()),
    'review': ('review', ()),
    'caller_integration': ('integration', ('S1', 'S2')),
    'recovery_points': ('restore', ('S3',)),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


def _read(path, limit=64 * 1024 * 1024):
    path = Path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError('file_type_or_size')
    data = path.read_bytes()
    if len(data) > limit:
        raise ValueError('file_size')
    return data


def _relative(root, value):
    if not isinstance(value, str) or '\\' in value:
        raise ValueError('file_path')
    path = Path(value)
    if path.is_absolute() or not path.parts or any(x in ('.', '..', '.git') for x in path.parts):
        raise ValueError('file_path')
    root = Path(root).resolve()
    cursor = root
    for part in path.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError('file_symlink')
    if not cursor.resolve().is_relative_to(root):
        raise ValueError('file_path')
    return cursor


def _hashed(root, row):
    if not isinstance(row, dict) or not re.fullmatch('[0-9a-f]{64}', str(row.get('sha256', ''))):
        raise ValueError('file_hash')
    data = _read(_relative(root, row.get('path')))
    if digest(data) != row['sha256']:
        raise ValueError('file_hash')
    return data


def _git(repo, *args):
    # Status remains observational, including repositories configured with fsmonitor.
    env = {**{k: v for k, v in os.environ.items() if not k.startswith('GIT_')},
           'GIT_OPTIONAL_LOCKS': '0', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': os.devnull}
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath='+os.devnull,
                             '-C', str(repo), *args], capture_output=True, env=env, timeout=30)
    if result.returncode:
        raise ValueError('git_read_failed')
    return result.stdout.decode('utf-8').strip()


def _tags(repo):
    text = _git(repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/tags/')
    return dict(line.split(' ', 1) for line in text.splitlines())


@dataclass
class Catalog:
    groups: dict
    scenarios: dict
    patches: list
    fingerprints: dict

    def requirements(self, candidate):
        if candidate not in ('first', 'enhanced'):
            raise ValueError('candidate')
        result = {key: dict(kind='integration', seams=seams)
                  for key, seams in {**self.groups, **self.scenarios}.items()
                  if candidate == 'enhanced' or key not in P5}
        result.update({key: dict(kind='integration', seams=['S1', 'S2'] if key != 'G01' else ['S1'])
                       for key in ('G01', 'G02', 'G03')})
        result.update({f'D{i:02}': dict(kind='integration', seams=['S1'])
                       for i in range(1, len(self.patches)+1)})
        result.update({key: dict(kind=kind, seams=list(seams)) for key, (kind, seams) in GATES.items()})
        if candidate == 'enhanced':
            result.update({key: dict(kind='integration', seams=['S1', 'S2']) for key in ('DEV45', 'DEV46')})
        return result


def load_catalog(spec, matrix, patches):
    data = {name: _read(path) for name, path in [('spec', spec), ('matrix', matrix), ('patches', patches)]}
    fingerprints = {key: digest(value) for key, value in data.items()}
    if fingerprints != INPUT_HASHES:
        raise ValueError('catalog_fingerprint')
    text = data['spec'].decode('utf-8')
    groups, scenarios = {}, {}
    for line in text.splitlines():
        columns = [x.strip() for x in line.split('|')]
        if len(columns) > 5 and re.match(r'^F\d{2} ', columns[1]):
            groups[columns[1][:3]] = re.findall(r'S[123]', columns[4])
        if len(columns) > 4 and re.fullmatch(r'AT\d{2}', columns[1]):
            scenarios[columns[1]] = re.findall(r'S[123]', columns[2])
    ledger = load_json(data['matrix']); dispositions = load_json(data['patches'])
    if (set(groups) != {f'F{i:02}' for i in range(1,50)}
            or set(scenarios) != {f'AT{i:02}' for i in range(1,31)}
            or {x['id'] for x in ledger['groups']} != set(groups)
            or len(ledger['groups']) != 49 or len(dispositions) != 19
            or len({x['path'] for x in dispositions}) != 19):
        raise ValueError('catalog_scope')
    return Catalog(groups, scenarios, dispositions, fingerprints)


def scaffold(repo, catalog, candidate, *, baseline=BASELINE):
    return dict(schema_version=1, candidate=candidate, inputs=catalog.fingerprints,
        source=dict(commit=_git(repo, 'rev-parse', 'HEAD'), baseline=baseline,
                    branch=BRANCH, tags=_tags(repo)),
        evidence=[], coverage={key: dict(status='pending', evidence=[])
                               for key in catalog.requirements(candidate)},
        permissions={action: dict(approved=False, evidence=[]) for action in ('commit','push','deploy')},
        observation=dict(started_at=None, ended_at=None, account_alias=None,
            unresolved_severe=None, actual_version_verified=False, previous_version=None,
            image_digest=None, data_checkpoint=None,
            scenarios={key: dict(status='pending', evidence=[]) for key in REAL_SCENARIOS}))


def _source(repo, source, baseline):
    head = _git(repo, 'rev-parse', 'HEAD')
    clean = not _git(repo, 'status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none')
    branch_ok = _git(repo, 'branch', '--show-current') == BRANCH == source.get('branch')
    commit_ok = source.get('commit') == head and bool(re.fullmatch('[0-9a-f]{40}', head))
    baseline_ok = source.get('baseline') == baseline
    try:
        _git(repo, 'merge-base', '--is-ancestor', baseline, head)
    except ValueError:
        baseline_ok = False
    tags = source.get('tags')
    current = _tags(repo)
    tags_ok = isinstance(tags, dict) and all(current.get(k) == v for k, v in tags.items())
    return dict(commit=head, clean=clean, branch_ok=branch_ok, commit_ok=commit_ok,
                baseline_ok=baseline_ok, old_tags_unchanged=tags_ok)


def _entrypoints(repo, entries):
    if not isinstance(entries, list):
        raise ValueError('entrypoints')
    roles = set()
    for row in entries:
        data = _hashed(repo, row)
        start, end = row.get('start_line'), row.get('end_line')
        if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(data.splitlines())
                or row.get('role') not in ('implementation', 'test')):
            raise ValueError('entrypoint_range')
        _git(repo, 'ls-files', '--error-unmatch', '--', row['path'])
        roles.add(row['role'])
    return roles


def _receipts(repo, root, manifest, head):
    receipts, failures = {}, []
    rows = manifest.get('evidence')
    if not isinstance(rows, list):
        return {}, ['evidence_inventory_invalid']
    seen = set()
    for index, row in enumerate(rows):
        try:
            identifier = row['id']
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', identifier) or identifier in seen:
                raise ValueError('id')
            seen.add(identifier)
            body = load_json(_hashed(root, row))
            if (body.get('schema_version') != 1 or body.get('id') != identifier
                    or body.get('commit') != head or body.get('candidate') != manifest.get('candidate')
                    or body.get('kind') not in KINDS
                    or body.get('status') != 'passed' or type(body.get('synthetic')) is not bool
                    or not isinstance(body.get('scope'), list) or not body['scope']
                    or not all(isinstance(x,str) for x in body['scope'])
                    or len(set(body['scope'])) != len(body['scope'])
                    or not isinstance(body.get('seams'), list)
                    or not set(body['seams']) <= {'S1','S2','S3'}):
                raise ValueError('receipt_contract')
            if body['kind'] in CHECK_KINDS:
                counts = body.get('checks', {})
                if (any(type(counts.get(k)) is not int for k in ('executed','failed','skipped'))
                        or counts['executed'] <= 0 or counts['failed'] != 0 or counts['skipped'] != 0):
                    raise ValueError('checks')
            attachments = body.get('attachments')
            if not isinstance(attachments, list) or not attachments:
                raise ValueError('attachments')
            for attachment in attachments:
                _hashed(root, attachment)
            roles = _entrypoints(repo, body.get('entrypoints', []))
            if body['kind'] == 'integration' and roles != {'implementation','test'}:
                raise ValueError('entrypoint_roles')
            receipts[identifier] = body
        except (KeyError, TypeError, ValueError, OSError, UnicodeError):
            failures.append(f'evidence_invalid:{index}')
    return receipts, failures


def _claim(row, scope, kind, seams, receipts):
    if not isinstance(row, dict) or row.get('status') != 'passed':
        return False
    refs = row.get('evidence')
    if not isinstance(refs, list) or not refs or any(not isinstance(x,str) for x in refs):
        return False
    if len(set(refs)) != len(refs):
        return False
    covered = set()
    for ref in refs:
        body = receipts.get(ref)
        if (not body or scope not in body['scope'] or body['kind'] != kind or body['synthetic']):
            return False
        covered.update(body['seams'])
    return set(seams) <= covered


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone')
    return result


def _observation(observation, receipts, now):
    failures = []
    try:
        start, end = _timestamp(observation['started_at']), _timestamp(observation['ended_at'])
        if (end-start).total_seconds() < 48*3600 or end > now:
            raise ValueError('observation_window')
    except (KeyError, TypeError, ValueError):
        return ['observation_window'], 0
    alias = observation.get('account_alias')
    if not isinstance(alias,str) or not re.fullmatch(r'account-[0-9]{2,6}',alias):
        failures.append('single_account_alias')
    if type(observation.get('unresolved_severe')) is not int or observation['unresolved_severe'] != 0:
        failures.append('unresolved_severe')
    if (observation.get('actual_version_verified') is not True
            or not re.fullmatch('[0-9a-f]{40}', str(observation.get('previous_version','')))
            or not re.fullmatch('sha256:[0-9a-f]{64}', str(observation.get('image_digest','')))
            or not re.fullmatch(r'checkpoint-[a-zA-Z0-9_-]{1,64}',str(observation.get('data_checkpoint','')))):
        failures.append('actual_version_or_recovery_point')
    scenarios = observation.get('scenarios', {})
    passed = 0
    for scenario in REAL_SCENARIOS:
        row = scenarios.get(scenario)
        good = _claim(row, 'real:'+scenario, 'runtime', [], receipts)
        if good:
            for ref in row['evidence']:
                body = receipts[ref]
                try:
                    good = good and body.get('account_alias') == alias and start <= _timestamp(body.get('occurred_at')) <= end
                except ValueError:
                    good = False
                required_fields = {
                    'observation_window': ('window_started_at','window_ended_at','unresolved_severe'),
                    'actual_deployed_version': ('previous_version','image_digest'),
                    'retained_image_checkpoint': ('image_digest','data_checkpoint'),
                }.get(scenario, ())
                expected = {**observation, 'window_started_at': observation['started_at'],
                            'window_ended_at': observation['ended_at']}
                good = good and all(body.get(field) == expected[field] for field in required_fields)
        if good:
            passed += 1
        else:
            failures.append('real_scene:'+scenario)
    if set(scenarios) != set(REAL_SCENARIOS):
        failures.append('real_scene_scope')
    return failures, passed


def evaluate(repo, evidence_root, manifest, catalog, *, baseline=BASELINE, now=None):
    """JSON-compatible allowlisted report. Never echo paths, receipts or exceptions."""
    failures = []
    try:
        if manifest.get('schema_version') != 1 or manifest.get('inputs') != catalog.fingerprints:
            failures.append('manifest_contract')
        requirements = catalog.requirements(manifest.get('candidate'))
        source = _source(repo, manifest.get('source',{}), baseline)
        for key in ('clean','branch_ok','commit_ok','baseline_ok','old_tags_unchanged'):
            if not source[key]:
                failures.append('source:'+key)
        receipts, invalid = _receipts(repo, evidence_root, manifest, source['commit'])
        failures.extend(invalid)
        coverage = manifest.get('coverage', {})
        if set(coverage) != set(requirements):
            failures.append('coverage_scope')
        missing = [key for key, rule in requirements.items()
                   if not _claim(coverage.get(key), key, rule['kind'], rule['seams'], receipts)]
        failures.extend('coverage:'+key for key in missing)
        build_digests = set()
        for ref in coverage.get('candidate_build', {}).get('evidence', []):
            build = receipts.get(ref, {})
            value = build.get('image_digest')
            if build.get('source_clean') is not True or not re.fullmatch('sha256:[0-9a-f]{64}', str(value)):
                failures.append('candidate_build_metadata')
            else:
                build_digests.add(value)
        if len(build_digests) != 1:
            failures.append('candidate_build_digest')
        code_verified = not failures
        permissions = manifest.get('permissions', {})
        permits = {}
        for action in ('commit','push','deploy'):
            row = permissions.get(action, {})
            refs = row.get('evidence', [])
            permits[action] = (row.get('approved') is True
                and _claim(dict(status='passed',evidence=refs), 'permit:'+action, 'approval', [], receipts)
                and all(receipts[ref].get('approved') is True for ref in refs))
            if not permits[action]:
                failures.append('permission:'+action)
        runtime_failures, real_count = _observation(manifest.get('observation',{}), receipts,
                                                    now or datetime.now(timezone.utc))
        failures.extend(runtime_failures)
        if manifest.get('observation', {}).get('image_digest') not in build_digests:
            failures.append('deployed_image_mismatch')
        final_source = _source(repo, manifest.get('source', {}), baseline)
        if final_source != source:
            failures.append('source:changed_during_check')
            code_verified = False
            source = final_source
        return dict(schema_version=1, candidate=manifest['candidate'], source=source,
            input_hashes=catalog.fingerprints, code_verified=code_verified,
            trial_ready=code_verified and all(permits.values()), release_accepted=not failures,
            coverage=dict(required=len(requirements), verified=len(requirements)-len(missing), missing=missing,
                          excluded=sorted(P5) if manifest['candidate']=='first' else []),
            evidence=dict(listed=len(manifest['evidence']), verified=len(receipts),
                          synthetic=sum(x['synthetic'] for x in receipts.values())),
            permissions=permits, observation=dict(required_hours=48, required_scenes=len(REAL_SCENARIOS),
                                                verified_scenes=real_count), failures=failures)
    except (AttributeError, KeyError, TypeError, ValueError, OSError, UnicodeError, subprocess.SubprocessError):
        return invalid_report()


def invalid_report():
    return dict(schema_version=1, code_verified=False, trial_ready=False,
                release_accepted=False, failures=['input_or_repository_invalid'])
