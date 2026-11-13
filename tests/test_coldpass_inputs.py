import copy
import json
import unittest
import hashlib
import shutil
import subprocess
import sys
from unittest.mock import patch

import coldfix as c
import test_coldpass_sources as fixtures


class ColdpassInputTests(unittest.TestCase):
    write = fixtures.ColdpassSourcesTests.write
    cli = fixtures.ColdpassSourcesTests.cli
    stored = fixtures.ColdpassSourcesTests.stored
    def setUp(self):
        fixtures.ColdpassSourcesTests.setUp(self)
        self.repo = self.root / 'upstream'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'behavior.py').write_text('value = 0\n')
        self.before = fixtures.commit(self.repo, 'before')
        (self.repo / 'behavior.py').write_text('value = 1\n')
        self.after = fixtures.commit(self.repo, 'fix')
        import coldpass_inputs as inputs
        self.acquisition = patch.object(inputs, 'acquire', create=True, return_value=(
            self.repo, dict(id=123, full_name='example/project', private=False, visibility='public')))
        self.acquisition.start()
        self.addCleanup(self.acquisition.stop)
        self.write([fixtures.run_row('bound-run', 'blocked', owner='repo',
                                    commit=self.before, what_failed=fixtures.SIBLING)])
        self.cli()
        self.failure = self.stored()[0]

    def entry(self):
        check = 'import unittest\nclass Import(unittest.TestCase):\n    def test_sibling(self):\n        self.assertEqual(1, 1)\n'
        return dict(failure_id=self.failure['id'], source_ref=self.failure['source_ref'],
                    type='coldpass', run_id='bound-run', repo='example/project', repository_id=123,
                    failure_class='sibling-import', before=self.before,
                    source_payload_sha256=c.digest(self.failure['error']),
                    model='fixture-model',
                    witness=dict(check=check, check_sha256=c.digest(check), cwd='.',
                                 argv=['python3', '-m', 'unittest', '-v', 'test_behavior.Import.test_sibling'],
                                 expected_failure='test_sibling (test_behavior.Import.test_sibling)',
                                 runtime=dict(name='python3', version=sys.version.split()[0], sha256=hashlib.sha256(__import__('pathlib').Path(shutil.which('python3')).read_bytes()).hexdigest()),
                                 setup=[], dependencies=[]),
                    upstream=dict(after=self.after, fix_commit=self.after,
                                  evidence='https://github.com/example/project/commit/' + self.after,
                                  patch_sha256=hashlib.sha256(c.privacy_git(self.repo, 'diff', '--binary', '--full-index', self.before, self.after)).hexdigest()))

    def test_coldpass_manifest_round_trip_binds_immutable_failure(self):
        import coldpass_inputs as inputs
        entry = self.entry()
        before = {p.name: p.read_bytes() for p in (self.store / 'failures').glob('*.json')}
        document = dict(schema_version=1, entries=[entry])
        inputs.import_document(self.store, document)
        self.assertEqual(inputs.load(self.store)['entries'], [entry])
        self.assertEqual(inputs.proposed_pairs(self.store)[0]['proof_input'], entry['witness'])
        self.assertEqual(inputs.proposed_pairs(self.store)[0]['before'], self.before)
        self.assertEqual(inputs.proposed_pairs(self.store)[0]['after'], self.after)
        saved = (self.store / inputs.NAME).read_bytes()
        inputs.import_document(self.store, document)
        self.assertEqual((self.store / inputs.NAME).read_bytes(), saved)
        self.assertEqual({p.name: p.read_bytes() for p in (self.store / 'failures').glob('*.json')}, before)

    def test_bad_bindings_and_commands_never_write(self):
        import coldpass_inputs as inputs
        entry = self.entry()
        for key, value in [('run_id', 'wrong'), ('repo', 'other/project'), ('before', 'e' * 40),
                           ('source_ref', 'wrong'), ('source_payload_sha256', '0' * 64)]:
            with self.subTest(key=key):
                bad = copy.deepcopy(entry)
                bad[key] = value
                with self.assertRaises(c.ColdfixError):
                    inputs.import_document(self.store, dict(schema_version=1, entries=[entry, bad]))
                self.assertFalse((self.store / inputs.NAME).exists())
        for change in ('missing', 'both', 'command'):
            bad = copy.deepcopy(entry)
            if change == 'missing':
                del bad['witness']
            elif change == 'both':
                bad['reproducer'] = bad['witness']
            else:
                bad['witness']['argv'] = ['coldpass', 'sibling-import']
            with self.assertRaises(c.ColdfixError):
                inputs.import_document(self.store, dict(schema_version=1, entries=[bad]))
            self.assertFalse((self.store / inputs.NAME).exists())

    def test_private_permissions_and_history(self):
        import coldpass_inputs as inputs
        inputs.import_document(self.store, dict(schema_version=1, entries=[self.entry()]))
        path = self.store / inputs.NAME
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        history = list((self.store / inputs.HISTORY).glob('*.json'))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].read_bytes(), path.read_bytes())
        self.assertEqual(history[0].stat().st_mode & 0o777, 0o600)

    def test_changed_proposal_rejected_without_replacing_history(self):
        import coldpass_inputs as inputs
        entry = self.entry()
        inputs.import_document(self.store, dict(schema_version=1, entries=[entry]))
        before = (self.store / inputs.NAME).read_bytes()
        entry['upstream']['after'] = 'e' * 40
        with self.assertRaises(c.ColdfixError):
            inputs.import_document(self.store, dict(schema_version=1, entries=[entry]))
        self.assertEqual((self.store / inputs.NAME).read_bytes(), before)
        self.assertEqual(len(list((self.store / inputs.HISTORY).glob('*.json'))), 1)

    def test_pairs_require_explicit_opt_in(self):
        import coldpass_inputs as inputs
        import learn_daily
        inputs.import_document(self.store, dict(schema_version=1, entries=[self.entry()]))
        ordinary = learn_daily.fix_pairs(self.store)
        self.assertFalse(any(row.get('type') == 'coldpass' for row in ordinary))
        proposed = learn_daily.fix_pairs(self.store, coldpass_inputs=True)
        self.assertEqual(proposed[-1]['status'], 'proposed')
        self.assertEqual(proposed[-1]['failure_id'], self.failure['id'])

    def test_unverified_public_identity_never_writes(self):
        for change in ({'id': 124}, {'private': True}, {'visibility': 'private'},
                       {'full_name': 'other/project'}, {'id': True}):
            with self.subTest(change=change):
                metadata = dict(id=123, full_name='example/project', private=False, visibility='public')
                metadata.update(change)
                __import__('coldpass_inputs').acquire.return_value = (self.repo, metadata)
                with self.assertRaisesRegex(c.ColdfixError, 'public_identity_unverified'):
                    __import__('coldpass_inputs').import_document(self.store, dict(schema_version=1, entries=[self.entry()]))
                self.assertFalse((self.store / 'coldpass-replay-inputs.json').exists())

    def test_unavailable_unrelated_and_metadata_fixes_never_write(self):
        import coldpass_inputs as inputs
        cases = []
        entry = self.entry()
        entry['upstream']['after'] = 'e' * 40
        cases.append((entry, 'revision_unavailable'))
        entry = self.entry()
        entry['upstream']['patch_sha256'] = '0' * 64
        cases.append((entry, 'fix_patch_unverified'))
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Example', '-c', 'user.email=example@example.invalid', 'commit', '--allow-empty', '-qm', 'empty'], check=True)
        empty = c.privacy_git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        entry = self.entry()
        entry['before'] = self.before
        entry['upstream']['fix_commit'] = empty
        entry['upstream']['after'] = empty
        cases.append((entry, 'fix_patch_unverified'))
        for entry, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(c.ColdfixError, reason):
                    inputs.import_document(self.store, dict(schema_version=1, entries=[entry]))
                self.assertFalse((self.store / inputs.NAME).exists())

    def test_missing_or_changed_pins_never_write(self):
        import coldpass_inputs as inputs
        for field in ('runtime', 'dependencies', 'setup', 'check'):
            entry = self.entry()
            if field == 'runtime':
                entry['witness'][field]['sha256'] = '0' * 64
            elif field == 'check':
                entry['witness']['check'] = 'not valid python !'
                entry['witness']['check_sha256'] = c.digest(entry['witness']['check'])
            else:
                entry['witness'][field] = [dict(path='missing.whl', version='1', sha256='0' * 64)]
            with self.subTest(field=field):
                with self.assertRaises(c.ColdfixError):
                    inputs.import_document(self.store, dict(schema_version=1, entries=[entry]))
                self.assertFalse((self.store / inputs.NAME).exists())

    def test_identical_tree_unrelated_history_and_metadata_only_rejected(self):
        import coldpass_inputs as inputs
        for kind in ('same_tree_not_fix', 'unrelated_fix_history', 'fix_patch_unverified'):
            subprocess.run(['git', '-C', str(self.repo), 'checkout', '-q', '--detach', self.before], check=True)
            if kind == 'same_tree_not_fix':
                (self.repo / 'behavior.py').write_text('value = 1\n')
                fixtures.commit(self.repo, 'change')
                (self.repo / 'behavior.py').write_text('value = 0\n')
            elif kind == 'unrelated_fix_history':
                subprocess.run(['git', '-C', str(self.repo), 'checkout', '-q', '--orphan', 'unrelated'], check=True)
                (self.repo / 'behavior.py').write_text('value = 2\n')
            else:
                (self.repo / 'model.json').write_text('{"model": "changed"}')
            after = fixtures.commit(self.repo, kind)
            entry = self.entry()
            entry['upstream'].update(after=after, fix_commit=after,
                evidence='https://github.com/example/project/commit/' + after,
                patch_sha256=hashlib.sha256(c.privacy_git(self.repo, 'diff', '--binary', '--full-index',
                                                        self.before, after)).hexdigest())
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(c.ColdfixError, kind):
                    inputs.import_document(self.store, dict(schema_version=1, entries=[entry]))
                self.assertFalse((self.store / inputs.NAME).exists())

    def test_colocated_test_only_fixes_never_write(self):
        import coldpass_inputs as inputs
        for name in ('cache_test.go', 'cache.test.js', 'cache.spec.ts', 'cache.test.tsx', 'behavior_test.py'):
            subprocess.run(['git', '-C', str(self.repo), 'checkout', '-q', '--detach', self.before], check=True)
            (self.repo / name).write_text('test fixture\n')
            after = fixtures.commit(self.repo, name)
            entry = self.entry()
            entry['upstream'].update(after=after, fix_commit=after,
                evidence='https://github.com/example/project/commit/' + after,
                patch_sha256=hashlib.sha256(c.privacy_git(self.repo, 'diff', '--binary', '--full-index',
                                                        self.before, after)).hexdigest())
            with self.subTest(name=name):
                with self.assertRaisesRegex(c.ColdfixError, 'fix_patch_unverified'):
                    inputs.import_document(self.store, dict(schema_version=1, entries=[entry]))
                self.assertFalse((self.store / inputs.NAME).exists())

    def test_witness_must_contain_the_named_behavioral_test(self):
        import coldpass_inputs as inputs
        for check in ('import unittest\n',
                      'class Import:\n    def test_sibling(self):\n        self.assertEqual(1, 1)\n',
                      'import unittest\nclass Import(unittest.TestCase):\n    def test_other(self):\n        self.assertEqual(1, 1)\n',
                      'import unittest\nclass Import(unittest.TestCase):\n    def test_sibling(self):\n        pass\n'):
            entry = self.entry()
            entry['witness'].update(check=check, check_sha256=c.digest(check))
            with self.subTest(check=check):
                with self.assertRaisesRegex(c.ColdfixError, 'unsupported_reproducer'):
                    inputs.import_document(self.store, dict(schema_version=1, entries=[entry]))
                self.assertFalse((self.store / inputs.NAME).exists())

    def test_unaudited_manifest_cannot_supply_pairs_and_identical_reimport_repairs(self):
        import coldpass_inputs as inputs
        document = dict(schema_version=1, entries=[self.entry()])
        c.atomic_create(self.store / inputs.NAME, c.encoded(document), mode=0o600)
        for read in (inputs.load, inputs.proposed_pairs):
            with self.assertRaisesRegex(c.ColdfixError, 'unaudited_manifest'):
                read(self.store)
        inputs.import_document(self.store, document)
        self.assertEqual(inputs.load(self.store), document)
        digest = c.digest(c.encoded(document))
        self.assertEqual((self.store / inputs.HISTORY / (digest + '.json')).read_text(), c.encoded(document))
        self.assertEqual(inputs.proposed_pairs(self.store)[0]['failure_id'], self.failure['id'])

    def test_reimport_cannot_launder_unaudited_extras(self):
        import coldpass_inputs as inputs
        bad = self.entry()
        bad['upstream']['patch_sha256'] = '0' * 64
        document = dict(schema_version=1, entries=[bad])
        c.atomic_create(self.store / inputs.NAME, c.encoded(document), mode=0o600)
        before = (self.store / inputs.NAME).read_bytes()
        with self.assertRaisesRegex(c.ColdfixError, 'fix_patch_unverified'):
            inputs.import_document(self.store, dict(schema_version=1, entries=[]))
        self.assertEqual((self.store / inputs.NAME).read_bytes(), before)
        self.assertFalse((self.store / inputs.HISTORY).exists())

    def test_mismatched_audit_snapshot_cannot_supply_pairs(self):
        import coldpass_inputs as inputs
        document = dict(schema_version=1, entries=[self.entry()])
        inputs.import_document(self.store, document)
        archive = self.store / inputs.HISTORY / (c.digest(c.encoded(document)) + '.json')
        archive.write_text(c.encoded(dict(schema_version=1, entries=[])))
        with self.assertRaisesRegex(c.ColdfixError, 'unaudited_manifest'):
            inputs.proposed_pairs(self.store)

    def test_external_artifacts_survive_input_deletion_and_detect_corruption(self):
        import coldpass_inputs as inputs
        external = self.root / 'external'
        external.mkdir()
        payload = b'\x00offline artifact\xff'
        (external / 'package.whl').write_bytes(payload)
        entry = self.entry()
        entry['witness']['dependencies'] = [dict(path='package.whl', version='1', sha256=hashlib.sha256(payload).hexdigest())]
        document = dict(schema_version=1, entries=[entry])
        inputs.import_document(self.store, document, artifacts=external)
        shutil.rmtree(external)
        proposal = inputs.proposed_pairs(self.store)[0]
        artifact = __import__('pathlib').Path(proposal['artifact_root']) / 'package.whl'
        self.assertTrue(artifact.resolve().is_relative_to(self.store.resolve()))
        self.assertEqual(artifact.read_bytes(), payload)
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
        self.assertEqual(artifact.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(inputs.load(self.store), document)
        inputs.import_document(self.store, document)
        artifact.write_bytes(b'corrupted')
        with self.assertRaisesRegex(c.ColdfixError, 'artifact_unavailable'):
            inputs.proposed_pairs(self.store)

    def test_invalid_document_does_not_retain_artifacts(self):
        import coldpass_inputs as inputs
        external = self.root / 'external'
        external.mkdir()
        (external / 'package.whl').write_bytes(b'wheel')
        entry = self.entry()
        entry['witness']['dependencies'] = [dict(path='package.whl', version='1', sha256=hashlib.sha256(b'wheel').hexdigest())]
        bad = copy.deepcopy(entry)
        bad['run_id'] = 'wrong'
        with self.assertRaises(c.ColdfixError):
            inputs.import_document(self.store, dict(schema_version=1, entries=[entry, bad]), artifacts=external)
        self.assertFalse((self.store / inputs.NAME).exists())
        self.assertFalse((self.store / 'coldpass-replay-artifacts').exists())

    def test_identical_preliminary_reimport_creates_history(self):
        import coldpass_inputs as inputs
        document = dict(schema_version=1, entries=[self.entry()])
        c.atomic_create(self.store / inputs.NAME, c.encoded(document), mode=0o600)
        inputs.import_document(self.store, document)
        self.assertEqual(len(list((self.store / inputs.HISTORY).glob('*.json'))), 1)
        self.assertEqual(inputs.load(self.store), document)

    def test_new_import_preserves_audited_history(self):
        import coldpass_inputs as inputs
        first = self.entry()
        digest = inputs.import_document(self.store, dict(schema_version=1, entries=[first]))
        archive = self.store / inputs.HISTORY / (digest + '.json')
        original = archive.read_bytes()
        self.write([fixtures.run_row('second-run', 'blocked', owner='repo',
                                    commit=self.before, what_failed=fixtures.SIBLING)])
        self.cli()
        self.failure = next(row for row in self.stored() if row['id'] != first['failure_id'])
        second = self.entry()
        second['run_id'] = 'second-run'
        inputs.import_document(self.store, dict(schema_version=1, entries=[second]))
        self.assertEqual(len(inputs.proposed_pairs(self.store)), 2)
        self.assertEqual(archive.read_bytes(), original)
        self.assertEqual(len(list((self.store / inputs.HISTORY).glob('*.json'))), 2)

    def test_artifact_symlink_escape_never_writes(self):
        import coldpass_inputs as inputs
        external = self.root / 'external'
        external.mkdir()
        outside = self.root / 'outside.whl'
        outside.write_bytes(b'wheel')
        (external / 'package.whl').symlink_to(outside)
        entry = self.entry()
        entry['witness']['dependencies'] = [dict(path='package.whl', version='1', sha256=hashlib.sha256(b'wheel').hexdigest())]
        with self.assertRaises(c.ColdfixError):
            inputs.import_document(self.store, dict(schema_version=1, entries=[entry]), artifacts=external)
        self.assertFalse((self.store / inputs.NAME).exists())
        self.assertFalse((self.store / inputs.HISTORY).exists())
        self.assertFalse((self.store / inputs.ARTIFACTS).exists())

    def test_artifact_aliases_import_idempotently(self):
        import coldpass_inputs as inputs
        external = self.root / 'alias-input'
        external.mkdir()
        payload = b'wheel'
        (external / 'package.whl').write_bytes(payload)
        entry = self.entry()
        entry['witness']['dependencies'] = [dict(path=name, version='1', sha256=hashlib.sha256(payload).hexdigest())
                                             for name in ('package.whl', './package.whl')]
        document = dict(schema_version=1, entries=[entry])
        first = inputs.import_document(self.store, document, artifacts=external)
        self.assertEqual(inputs.import_document(self.store, document, artifacts=external), first)
        proposal = inputs.proposed_pairs(self.store)[0]
        self.assertEqual((__import__('pathlib').Path(proposal['artifact_root']) / 'package.whl').read_bytes(), payload)
