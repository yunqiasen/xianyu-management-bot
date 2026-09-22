"""合成临时仓库与回执；这里的成功断言不是实际发布证据。"""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / 'docs/provenance'


def dump(path, data):
    path.write_text(json.dumps(data), encoding='utf-8')
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GateTests(unittest.TestCase):
    def setUp(self):
        from tools.release import checker
        self.gate = checker
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.evidence = self.root / 'evidence'; self.evidence.mkdir()
        self.git('init', '-b', 'xianyu-management-bot-fork')
        self.git('config', 'user.name', 'Fixture'); self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'app.py').write_text('def entry():\n    return 1\n')
        (self.repo / 'test_app.py').write_text('def test_entry():\n    assert True\n')
        self.git('add', '.'); self.git('commit', '-m', 'synthetic test only')
        self.commit = self.git('rev-parse', 'HEAD')
        self.git('tag', 'old-release')
        self.catalog = checker.load_catalog(ROOT/'docs/specs/XYMB-SPEC-001.md', ASSETS/'feature-coverage-matrix.json', ASSETS/'local-patch-dispositions.json')
        # Injection applies only to temporary test repository; CLI has no baseline override.
        self.manifest = checker.scaffold(self.repo, self.catalog, 'first', baseline=self.commit)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def report(self):
        return self.gate.evaluate(self.repo, self.evidence, self.manifest, self.catalog, baseline=self.commit,
            now=datetime(2026,9,21,tzinfo=timezone.utc))

    def receipt(self, scope, kind, seams=(), synthetic=False, **extra):
        identifier = f'ev{len(self.manifest["evidence"]):04d}'
        body = dict(schema_version=1, id=identifier, candidate=self.manifest["candidate"], commit=self.commit, scope=[scope], kind=kind,
                    seams=list(seams), status='passed', synthetic=synthetic,
                    checks=dict(executed=1, failed=0, skipped=0), attachments=[], entrypoints=[],
                    source_clean=True,image_digest='sha256:'+'2'*64)
        if kind == 'integration':
            for path, role in [('app.py','implementation'), ('test_app.py','test')]:
                body['entrypoints'].append(dict(path=path, role=role, start_line=1, end_line=2,
                    sha256=hashlib.sha256((self.repo/path).read_bytes()).hexdigest()))
        body.update(extra)
        trace = self.evidence/f'{identifier}.txt'
        trace.write_text('Synthetic test attachment; not operational evidence.\n')
        body['attachments'] = [dict(path=trace.name, sha256=hashlib.sha256(trace.read_bytes()).hexdigest())]
        sha = dump(self.evidence/f'{identifier}.json', body)
        self.manifest['evidence'].append(dict(id=identifier, path=f'{identifier}.json', sha256=sha))
        return identifier

    def complete_code(self):
        for scope, rule in self.catalog.requirements('first').items():
            ref = self.receipt(scope, rule['kind'], rule['seams'])
            self.manifest['coverage'][scope] = dict(status='passed', evidence=[ref])
        return self.report()

    def rewrite(self, ref, **changes):
        row=next(x for x in self.manifest['evidence'] if x['id']==ref)
        path=self.evidence/row['path']; body=json.loads(path.read_text()); body.update(changes)
        row['sha256']=dump(path,body)

    def test_cli_initializes_from_a_fresh_checkout_without_workspace_notes(self):
        import shutil
        shutil.copytree(ROOT / 'tools/release', self.repo / 'tools/release',
                        ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'docs/specs', self.repo / 'docs/specs')
        if (ROOT / 'docs/provenance').is_dir():
            shutil.copytree(ROOT / 'docs/provenance', self.repo / 'docs/provenance')
        result = subprocess.run([sys.executable, '-m', 'tools.release', 'init',
            '--candidate', 'enhanced'], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest['candidate'], 'enhanced')
        self.assertIn('F49', manifest['coverage'])
        self.assertIn('AT30', manifest['coverage'])
        self.assertIn('D19', manifest['coverage'])
        self.assertTrue(all(row['status'] == 'pending' for row in manifest['coverage'].values()))

    def test_scaffold_is_pending_and_pins_complete_scope(self):
        self.assertEqual(len(self.catalog.groups),49)
        self.assertEqual(len(self.catalog.scenarios),30)
        self.assertEqual(len(self.catalog.patches),19)
        self.assertFalse(self.report()['code_verified'])
        self.assertFalse(self.report()['release_accepted'])
        self.assertNotIn('AT21',self.manifest['coverage'])
        self.assertIn('AT21',self.gate.scaffold(self.repo,self.catalog,'enhanced',baseline=self.commit)['coverage'])

    def test_complete_offline_receipts_never_imply_real_acceptance(self):
        r=self.complete_code()
        self.assertTrue(r['code_verified'],r['failures'])
        self.assertFalse(r['release_accepted'])
        self.assertFalse(r['trial_ready'])

    def test_dirty_untracked_and_staged_files_block_code(self):
        self.complete_code()
        for mode in ('untracked','tracked','staged'):
            with self.subTest(mode=mode):
                path=self.repo/('new.txt' if mode=='untracked' else 'app.py')
                path.write_text('SECRET_TEST_VALUE')
                if mode=='staged':self.git('add','app.py')
                r=self.report(); self.assertFalse(r['code_verified']); self.assertNotIn('SECRET_TEST_VALUE',json.dumps(r))
                if mode=='untracked':path.unlink()
                else:self.git('restore','--staged','--worktree','app.py')

    def test_wrong_commit_branch_and_baseline_block(self):
        self.complete_code()
        self.manifest['source']['commit']='0'*40
        self.assertFalse(self.report()['code_verified'])
        self.manifest['source']['commit']=self.commit
        self.git('checkout','-b','wrong-branch')
        self.assertFalse(self.report()['code_verified'])

    def test_old_tag_movement_and_deletion_block(self):
        self.complete_code(); self.git('tag','-d','old-release')
        self.assertFalse(self.report()['code_verified'])

    def test_tampered_missing_and_escaping_evidence_block(self):
        self.complete_code(); row=self.manifest['evidence'][0]; old=row.copy()
        for path in ('missing.json','../secret.json','/etc/passwd'):
            row['path']=path
            self.assertFalse(self.report()['code_verified'])
        row.update(old); (self.evidence/row['path']).write_text('SECRET_TEST_VALUE')
        r=self.report(); self.assertFalse(r['code_verified']); self.assertNotIn('SECRET_TEST_VALUE',json.dumps(r))

    def test_symlink_evidence_blocked(self):
        self.complete_code(); row=self.manifest['evidence'][0]
        target=self.evidence/row['path']; moved=self.root/'secret.json'; target.rename(moved); target.symlink_to(moved)
        self.assertFalse(self.report()['code_verified'])

    def test_scope_seams_status_commit_checks_and_unit_claim_block(self):
        self.complete_code(); ref=self.manifest['coverage']['F06']['evidence'][0]
        original=json.loads((self.evidence/f'{ref}.json').read_text())
        variants=[dict(scope=['F07']),dict(seams=['S1']),dict(status='pending'),dict(commit='0'*40),
                  dict(kind='unit'),dict(checks=dict(executed=0,failed=0,skipped=0)),
                  dict(checks=dict(executed=3,failed=0,skipped=1)),dict(synthetic=True)]
        for change in variants:
            with self.subTest(change=change):
                self.rewrite(ref,**change); self.assertFalse(self.report()['code_verified']); self.rewrite(ref,**original)

    def test_entrypoint_hash_line_range_and_test_role_are_verified(self):
        self.complete_code(); ref=self.manifest['coverage']['F06']['evidence'][0]
        original=json.loads((self.evidence/f'{ref}.json').read_text())['entrypoints']
        for key,value in [('sha256','0'*64),('end_line',999),('path','../secret'),('role','implementation')]:
            points=copy.deepcopy(original); points[1][key]=value
            self.rewrite(ref,entrypoints=points); self.assertFalse(self.report()['code_verified'])
        self.rewrite(ref,entrypoints=original); self.assertTrue(self.report()['code_verified'])

    def test_attachments_hashes_are_verified(self):
        self.complete_code(); ref=self.manifest['coverage']['F06']['evidence'][0]
        sha=dump(self.evidence/'trace.json',{'result':'fixture'})
        self.rewrite(ref,attachments=[dict(path='trace.json',sha256=sha)])
        self.assertTrue(self.report()['code_verified'])
        (self.evidence/'trace.json').write_text('changed')
        self.assertFalse(self.report()['code_verified'])

    def test_duplicate_or_unknown_scope_and_duplicate_evidence_block(self):
        self.complete_code(); self.manifest['coverage']['AT99']=dict(status='passed',evidence=[])
        self.assertFalse(self.report()['code_verified'])
        del self.manifest['coverage']['AT99']; self.manifest['evidence'].append(self.manifest['evidence'][0])
        self.assertFalse(self.report()['code_verified'])

    def test_enhanced_requires_dev45_dev46_and_all_p5_scenarios(self):
        self.complete_code(); self.manifest['candidate']='enhanced'
        r=self.report(); self.assertFalse(r['code_verified'])
        self.assertTrue({'AT21','AT22','AT23','AT24','DEV45','DEV46'} <= set(r['coverage']['missing']))

    def complete_runtime(self):
        self.complete_code()
        for action in ('commit','push','deploy'):
            ref=self.receipt('permit:'+action,'approval',approved=True)
            self.manifest['permissions'][action]=dict(approved=True,evidence=[ref])
        obs=self.manifest['observation']
        obs.update(started_at='2026-09-18T00:00:00+00:00', ended_at='2026-09-20T00:00:00+00:00',
                   account_alias='account-01', unresolved_severe=0, actual_version_verified=True,
                   previous_version='1'*40, image_digest='sha256:'+'2'*64, data_checkpoint='checkpoint-01')
        for scenario in self.gate.REAL_SCENARIOS:
            ref=self.receipt('real:'+scenario,'runtime',['S1','S2','S3'],account_alias='account-01',
                             occurred_at='2026-09-19T00:00:00+00:00',
                             window_started_at=obs['started_at'],window_ended_at=obs['ended_at'],
                             unresolved_severe=0,previous_version=obs['previous_version'],
                             image_digest=obs['image_digest'],data_checkpoint=obs['data_checkpoint'])
            obs['scenarios'][scenario]=dict(status='passed',evidence=[ref])
        return self.report()

    def test_real_gate_needs_time_scenarios_and_three_independent_permissions(self):
        r=self.complete_runtime(); self.assertTrue(r['release_accepted'],r['failures'])
        for action in ('commit','push','deploy'):
            self.manifest['permissions'][action]['approved']=False
            self.assertFalse(self.report()['release_accepted'])
            self.manifest['permissions'][action]['approved']=True
        self.manifest['observation']['ended_at']='2026-09-19T23:59:59+00:00'
        self.assertFalse(self.report()['release_accepted'])

    def test_real_fixture_missing_scene_and_severe_incident_block(self):
        self.complete_runtime(); obs=self.manifest['observation']
        ref=obs['scenarios']['messages']['evidence'][0]
        self.rewrite(ref,synthetic=True); self.assertFalse(self.report()['release_accepted'])
        self.rewrite(ref,synthetic=False); obs['unresolved_severe']=1
        self.assertFalse(self.report()['release_accepted'])
        obs['unresolved_severe']=0; del obs['scenarios']['messages']
        self.assertFalse(self.report()['release_accepted'])

    def test_runtime_wrong_account_outside_window_and_future_window_block(self):
        self.complete_runtime(); obs=self.manifest['observation']; ref=obs['scenarios']['messages']['evidence'][0]
        self.rewrite(ref,account_alias='account-02'); self.assertFalse(self.report()['release_accepted'])
        self.rewrite(ref,account_alias='account-01',occurred_at='2026-09-17T00:00:00+00:00')
        self.assertFalse(self.report()['release_accepted'])
        obs['ended_at']='2099-09-20T00:00:00+00:00'; self.assertFalse(self.report()['release_accepted'])

    def test_missing_catalog_or_shrunken_matrix_fails_closed(self):
        altered=json.loads((ASSETS/'feature-coverage-matrix.json').read_text()); altered['groups'].pop()
        path=self.root/'matrix.json'; dump(path,altered)
        with self.assertRaises(ValueError):
            self.gate.load_catalog(ROOT/'docs/specs/XYMB-SPEC-001.md',path,ASSETS/'local-patch-dispositions.json')

    def test_cli_emits_redacted_json_for_malformed_input(self):
        path=self.root/'SECRET_TEST_VALUE.json'; path.write_text('{SECRET_TEST_VALUE')
        result=subprocess.run([sys.executable,'-m','tools.release','check','--manifest',str(path),
            '--evidence-root',str(self.evidence)],cwd=ROOT,text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(json.loads(result.stdout)['release_accepted'])
        self.assertNotIn('SECRET_TEST_VALUE',result.stdout+result.stderr)

    def test_edited_observation_metadata_requires_hashed_window_receipt(self):
        self.complete_runtime()
        self.manifest['observation']['started_at']='2026-09-17T00:00:00+00:00'
        self.assertFalse(self.report()['release_accepted'])

    def test_actual_image_checkpoint_and_previous_version_match_receipts(self):
        self.complete_runtime(); obs=self.manifest['observation']
        for field,value in [('image_digest','sha256:'+'3'*64),('data_checkpoint','checkpoint-other'),('previous_version','4'*40)]:
            with self.subTest(field=field):
                old=obs[field]; obs[field]=value
                self.assertFalse(self.report()['release_accepted']); obs[field]=old

    def test_one_permission_receipt_does_not_authorize_other_actions(self):
        self.complete_runtime()
        self.manifest['permissions']['deploy']['evidence']=self.manifest['permissions']['commit']['evidence']
        self.assertFalse(self.report()['trial_ready'])

    def test_receipts_for_another_candidate_are_not_reusable(self):
        self.complete_code(); ref=self.manifest['coverage']['F01']['evidence'][0]
        self.rewrite(ref,candidate='enhanced')
        self.assertFalse(self.report()['code_verified'])

    def test_moved_tag_object_is_detected(self):
        self.complete_code()
        (self.repo/'new.py').write_text('pass\n');self.git('add','.');self.git('commit','-m','another fixture')
        self.git('tag','-f','old-release')
        self.assertFalse(self.report()['source']['old_tags_unchanged'])

    def test_empty_attachment_and_nonregular_file_are_rejected(self):
        self.complete_code(); ref=self.manifest['coverage']['F01']['evidence'][0]
        self.rewrite(ref,attachments=[])
        self.assertFalse(self.report()['code_verified'])
        self.rewrite(ref,attachments=[dict(path='.',sha256='0'*64)])
        self.assertFalse(self.report()['code_verified'])

    def test_cli_pending_scaffold_fails_acceptance_without_mutating_repository(self):
        path=self.root/'manifest.json';dump(path,self.manifest)
        before=self.git('status','--porcelain');tags=self.git('show-ref','--tags')
        result=subprocess.run([sys.executable,'-m','tools.release','check','--repo',str(self.repo),
            '--manifest',str(path),'--evidence-root',str(self.evidence)],cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(result.returncode,1)
        self.assertFalse(json.loads(result.stdout)['release_accepted'])
        self.assertEqual(before,self.git('status','--porcelain'))
        self.assertEqual(tags,self.git('show-ref','--tags'))

    def test_duplicate_json_keys_and_invalid_args_emit_json(self):
        path=self.root/'manifest.json'; path.write_text('{"schema_version":1,"schema_version":1}')
        for args in (['check','--manifest',str(path),'--evidence-root',str(self.evidence)],['SECRET_TEST_VALUE']):
            result=subprocess.run([sys.executable,'-m','tools.release',*args],cwd=ROOT,text=True,capture_output=True)
            self.assertEqual(result.returncode,2);self.assertFalse(json.loads(result.stdout)['release_accepted'])
            self.assertNotIn('SECRET_TEST_VALUE',result.stdout+result.stderr)

    def test_worktree_change_during_check_blocks(self):
        from unittest.mock import patch
        self.complete_runtime()
        original=self.gate._observation
        def mutate(*args):
            (self.repo/'app.py').write_text('changed during gate\n')
            return original(*args)
        with patch.object(self.gate,'_observation',side_effect=mutate):
            self.assertFalse(self.report()['release_accepted'])

    def test_malformed_shapes_fail_closed_in_library(self):
        for value in ([],None,'SECRET_TEST_VALUE',{'candidate':'first','schema_version':1,'coverage':[]}):
            with self.subTest(shape=type(value)):
                result=self.gate.evaluate(self.repo,self.evidence,value,self.catalog,baseline=self.commit)
                self.assertFalse(result['release_accepted'])

    def test_candidate_build_records_clean_source_and_image_digest(self):
        self.assertIn('candidate_build',self.catalog.requirements('first'))
        self.complete_code(); ref=self.manifest['coverage']['candidate_build']['evidence'][0]
        self.rewrite(ref,source_clean=False)
        self.assertFalse(self.report()['code_verified'])

    def test_deployed_image_must_be_the_verified_candidate_build(self):
        self.assertIn('candidate_build',self.catalog.requirements('first'))
        self.complete_runtime(); ref=self.manifest['coverage']['candidate_build']['evidence'][0]
        self.rewrite(ref,image_digest='sha256:'+'9'*64)
        self.assertFalse(self.report()['release_accepted'])

    def test_ambient_git_overrides_do_not_redirect_repository_checks(self):
        import os
        from unittest.mock import patch
        self.complete_code()
        with patch.dict(os.environ,{'GIT_WORK_TREE':str(self.evidence),'GIT_DIR':str(self.evidence/'absent')}):
            self.assertTrue(self.report()['code_verified'])
