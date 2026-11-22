import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import coldfix as c
import learn_replay
import public_admission


class PublicAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = self.root / 'sample-project'
        self.fixture.mkdir()
        self.git('init', '-q', str(self.fixture))
        self.git('-C', str(self.fixture), 'config', 'user.email', 'fixture@example.invalid')
        self.git('-C', str(self.fixture), 'config', 'user.name', 'Fixture')
        (self.fixture / 'value').write_text('before')
        self.git('-C', str(self.fixture), 'add', '.')
        self.git('-C', str(self.fixture), 'commit', '-qm', 'before')
        self.before = self.git('-C', str(self.fixture), 'rev-parse', 'HEAD')
        (self.fixture / 'value').write_text('after')
        self.git('-C', str(self.fixture), 'commit', '-qam', 'after')
        self.after = self.git('-C', str(self.fixture), 'rev-parse', 'HEAD')
        self.entry = dict(repo='Public/Sample-Project', repository_id=123, before=self.before, after=self.after)
        self.failure = dict(repo='Public/Sample-Project', error=json.dumps(dict(type='coldpass', commit=self.before,
                                                                repository_id=123)))
        self.commands = []

    def git(self, *args):
        return subprocess.run(['git', *args], capture_output=True, text=True, check=True).stdout.strip()

    def manifest(self):
        payload = dict(version=1, entries=[self.entry])
        payload['digest'] = hashlib.sha256(c.encoded(payload).encode()).hexdigest()
        (self.root / public_admission.PATH).write_text(c.encoded(payload))

    def transport(self, command, timeout, env):
        self.commands.append((command, env))
        self.assertEqual(command[1], 'clone')
        self.assertEqual(command[-2], 'https://github.com/Public/Sample-Project.git')
        shutil.copytree(self.fixture, command[-1], dirs_exist_ok=True)
        self.git('-C', command[-1], 'config', '--remove-section', 'user')
        self.git('-C', command[-1], 'remote', 'add', 'origin', command[-2])

    def daily(self, failure=None):
        def drive(root, ledger, sources, pairs_path, timeout):
            return sources.get(failure or self.failure, (self.before, self.after))
        with patch.object(learn_replay, 'run_in_workspace', side_effect=drive):
            return learn_replay.run(self.root, {})

    def test_daily_coldpass_public_admission_uses_credential_free_mirror(self):
        local = self.root / 'Sample-Project'
        shutil.copytree(self.fixture, local)
        self.git('-C', str(local), 'remote', 'add', 'origin', 'https://github.com/Public/Sample-Project.git')
        self.manifest()
        with patch.object(public_admission, 'identity') as identity, \
                patch.object(learn_replay, 'clone_source', side_effect=self.transport), \
                patch.dict(os.environ, COLDFIX_OWNER='Owner', COLDFIX_REPLAY_PEERS='/private/peer',
                           COLDFIX_CHECKOUTS=str(self.root)):
            self.daily()
        self.assertEqual(identity.call_count, 3)
        self.assertEqual(len(self.commands), 1)
        command, env = self.commands[0]
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)
        self.assertEqual(env['GIT_CONFIG_NOSYSTEM'], '1')
        self.assertEqual(env['GIT_CONFIG_KEY_0'], 'credential.helper')
        self.assertEqual(env['GIT_CONFIG_VALUE_0'], '')
        self.assertNotIn('refs/pull', ' '.join(command))
        self.assertTrue(env['HOME'].endswith('/.public-home'))

    def test_unbounded_and_local_shortcut_deny_before_clone(self):
        local = self.root / 'Sample-Project'
        shutil.copytree(self.fixture, local)
        self.git('-C', str(local), 'remote', 'add', 'origin', 'https://github.com/Public/Sample-Project.git')
        with patch.object(learn_replay, 'clone_source') as clone, \
                patch.dict(os.environ, COLDFIX_OWNER='Owner', COLDFIX_CHECKOUTS=str(self.root)):
            with self.assertRaisesRegex(c.ColdfixError, 'not explicitly admitted'):
                self.daily()
            clone.assert_not_called()

    def test_unverified_private_or_renamed_identity_denies(self):
        self.manifest()
        for metadata in ({'private': True}, {'private': False, 'visibility': 'public',
                          'full_name': 'Other/Sample-Project', 'id': 123}, {}):
            with self.subTest(metadata=metadata):
                with patch.object(public_admission.urllib.request, 'build_opener') as opener:
                    response = opener.return_value.open.return_value.__enter__.return_value
                    response.url = 'https://api.github.com/repos/Public/Sample-Project'
                    response.read.return_value = json.dumps(metadata).encode()
                    with self.assertRaises(c.ColdfixError):
                        public_admission.identity('Public/Sample-Project', 123)

    def test_poisoned_cache_denies(self):
        workspace = self.root / 'workspace'
        clone = workspace / 'public/public/sample-project'
        clone.parent.mkdir(parents=True)
        shutil.copytree(self.fixture, clone)
        self.git('-C', str(clone), 'remote', 'add', 'origin', 'https://token@github.com/Public/Sample-Project.git')
        sources = learn_replay.SourceProjects(None, workspace, 'Owner', 30)
        with patch.object(learn_replay, 'clone_source') as transport:
            with self.assertRaisesRegex(c.ColdfixError, 'unsafe public cache'):
                sources.mirror('Public/Sample-Project', [self.before], True)
            transport.assert_not_called()

    def test_peer_fallback_disabled(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        sources = learn_replay.SourceProjects(None, workspace, 'Owner', 30)
        sources.peers = ['/private/peer']
        with patch.object(learn_replay, 'clone_source', side_effect=subprocess.CalledProcessError(1, 'git')) as clone:
            with self.assertRaises(subprocess.CalledProcessError):
                sources.mirror('Public/Sample-Project', [self.before], True)
            self.assertEqual(clone.call_count, 1)

    def tracking_transport(self, command, timeout, env):
        self.commands.append((command, env))
        subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', '--no-checkout', '--no-tags', '--',
                        str(self.fixture), command[-1]], env=env, check=True, capture_output=True)
        self.git('-C', command[-1], 'remote', 'set-url', 'origin', command[-2])

    def test_fresh_clone_tracks_any_valid_default_branch(self):
        for branch in ('trunk', 'develop', 'release/stable', 'Feature.Mixed'):
            with self.subTest(branch=branch):
                self.git('-C', str(self.fixture), 'branch', '-m', branch)
                sources = learn_replay.SourceProjects(None, self.root / branch, 'Owner', 30,
                                                     admissions={self.entry['repo']: self.entry})
                with patch.object(public_admission, 'identity'), \
                        patch.object(learn_replay, 'clone_source', side_effect=self.tracking_transport):
                    repo = sources.get(self.failure, (self.before, self.after))
                    self.assertEqual(self.git('-C', str(repo), 'config', 'branch.' + branch + '.remote'), 'origin')
                    self.assertEqual(self.git('-C', str(repo), 'config', 'branch.' + branch + '.merge'),
                                     'refs/heads/' + branch)
                    self.assertEqual(self.git('-C', str(repo), 'rev-parse', self.after), self.after)
                    self.git('-C', str(repo), 'config', 'branch.' + branch + '.merge', 'refs/heads/upstream')
                    self.assertEqual(sources.get(self.failure, (self.before, self.after)), repo)

    def test_history_discovery_skips_unadmitted_intermediate_commit(self):
        (self.fixture / 'value').write_text('final')
        (self.fixture / 'tests').mkdir()
        (self.fixture / 'tests/test_value.py').write_text(
            'import unittest\nclass ValueTests(unittest.TestCase):\n    def test_value(self):\n        pass\n')
        self.git('-C', str(self.fixture), 'add', '.')
        self.git('-C', str(self.fixture), 'commit', '-qm', 'final')
        self.entry['after'] = self.git('-C', str(self.fixture), 'rev-parse', 'HEAD')
        self.manifest()
        self.failure['error'] = json.dumps(dict(type='coldpass', commit=self.before, repository_id=123,
                                               log_tail='FAIL: test_value (test_value.ValueTests)'))
        failure = c.event_record(dict(self.failure, command='python3 -m unittest discover -s tests',
            agent='fixture', kind='test', source_ref='history-fixture', occurred='2026-11-10T00:00:00Z'), 'manual')
        c.save(self.root, 'failures', failure)
        entry = dict(verdict='PASS', evidence=[failure['id']], headline='Preserve values',
                     draft=dict(kind='test', title='Preserve values', rule='Preserve values.'))
        ledger = dict(drafts=dict(fixture=entry))
        missing_pairs = self.root / 'missing-pairs.json'
        with patch.object(public_admission, 'identity'), \
                patch.object(learn_replay, 'clone_source', side_effect=self.transport), \
                patch.dict(os.environ, COLDFIX_OWNER='Owner', COLDFIX_CHECKOUTS=str(self.root)), \
                patch.object(learn_replay, 'admission_args',
                             side_effect=RuntimeError('admitted pair reached')) as admit:
            counts = learn_replay.run(self.root, ledger, pairs_path=missing_pairs, timeout=30)
        self.assertFalse(missing_pairs.exists())
        self.assertEqual(admit.call_count, 0)
        self.assertEqual(counts['proven'], 0)
        self.assertEqual(counts['rejected'], 1)
        self.assertIn('missing_upstream_fix', entry['replay']['reason'])
        sources = learn_replay.SourceProjects(None, self.root / 'guard-workspace', 'Owner', 30,
                                             admissions={self.entry['repo']: self.entry})
        with patch.object(sources, 'mirror') as mirror:
            with self.assertRaisesRegex(c.ColdfixError, 'revisions are not explicitly admitted'):
                sources.get(failure, (self.before, self.after))
            mirror.assert_not_called()

    def test_cached_replacement_ref_denies_before_reading_commit_contents(self):
        sources = learn_replay.SourceProjects(None, self.root / 'workspace', 'Owner', 30,
                                             admissions={self.entry['repo']: self.entry})
        with patch.object(public_admission, 'identity'), \
                patch.object(learn_replay, 'clone_source', side_effect=self.tracking_transport):
            repo = sources.get(self.failure, (self.before, self.after))
        self.git('-C', str(repo), 'replace', self.before, self.after)
        self.assertEqual(self.git('-C', str(repo), 'show', self.before + ':value'), 'after')
        self.git('-C', str(repo), 'pack-refs', '--all')
        with patch.object(public_admission, 'identity'), \
                patch.object(learn_replay, 'clone_source') as transport, \
                patch.object(sources, 'present', wraps=sources.present) as present:
            with self.assertRaisesRegex(c.ColdfixError, 'unsafe public cache'):
                acquired = learn_replay.source_repo(self.failure, sources, (self.before, self.after))
                self.git('-C', str(acquired), 'show', self.before + ':value')
            transport.assert_not_called()
            present.assert_not_called()

    def assert_private_object_cache_rejected(self, kind, after_fetch=False):
        private = self.root / 'private'
        shutil.copytree(self.fixture, private)
        (private / 'value').write_text('private contents')
        self.git('-C', str(private), 'commit', '-qam', 'private')
        private_sha = self.git('-C', str(private), 'rev-parse', 'HEAD')
        if kind == 'commondir':
            self.git('-C', str(private), 'config', '--remove-section', 'user')
            self.git('-C', str(private), 'remote', 'add', 'origin', 'https://github.com/Public/Sample-Project.git')
        if kind.startswith('pack'):
            self.git('-C', str(private), 'gc', '--prune=now')
        sources = learn_replay.SourceProjects(None, self.root / 'workspace', 'Owner', 30,
                                             admissions={self.entry['repo']: self.entry})
        with patch.object(public_admission, 'identity'), \
                patch.object(learn_replay, 'clone_source', side_effect=self.tracking_transport):
            repo = sources.get(self.failure, (self.before, self.after))

        def contaminate(*args):
            objects = repo / '.git/objects'
            private_objects = private / '.git/objects'
            if kind == 'commondir':
                (repo / '.git/commondir').write_text(str(private / '.git'))
            elif kind == 'pack-directory':
                shutil.rmtree(objects / 'pack')
                (objects / 'pack').symlink_to(private_objects / 'pack', target_is_directory=True)
            elif kind == 'pack-file':
                for source in (private_objects / 'pack').iterdir():
                    (objects / 'pack' / source.name).symlink_to(source)
            elif kind == 'loose-directory':
                for directory in private_objects.iterdir():
                    if len(directory.name) != 2:
                        continue
                    path = objects / directory.name
                    if path.exists():
                        shutil.rmtree(path)
                    path.symlink_to(directory, target_is_directory=True)
            else:
                for directory in private_objects.iterdir():
                    if len(directory.name) != 2:
                        continue
                    (objects / directory.name).mkdir(exist_ok=True)
                    for source in directory.iterdir():
                        path = objects / directory.name / source.name
                        if not path.exists():
                            if kind == 'hardlink':
                                os.link(source, path)
                            else:
                                path.symlink_to(source)

        if not after_fetch:
            contaminate()
            self.assertTrue(sources.present(repo, private_sha, True))
            self.assertEqual(self.git('-C', str(repo), 'show', private_sha + ':value'), 'private contents')
        else:
            learn_replay.REFRESHED.discard(repo)
        with patch.object(public_admission, 'identity'), \
                patch.object(learn_replay, 'clone_source', side_effect=contaminate) as transport, \
                patch.object(sources, 'present', wraps=sources.present) as present:
            with self.assertRaisesRegex(c.ColdfixError, 'unsafe public cache'):
                learn_replay.source_repo(self.failure, sources, (self.before, self.after))
            present.assert_not_called()
            self.assertEqual(transport.call_count, int(after_fetch))

    def test_cache_rejects_private_pack_directory_symlink_before_reads(self):
        self.assert_private_object_cache_rejected('pack-directory')

    def test_cache_rejects_private_pack_file_symlink_before_reads(self):
        self.assert_private_object_cache_rejected('pack-file')

    def test_cache_rejects_private_loose_directory_symlink_before_reads(self):
        self.assert_private_object_cache_rejected('loose-directory')

    def test_cache_rejects_private_loose_file_symlink_before_reads(self):
        self.assert_private_object_cache_rejected('loose-file')

    def test_cache_rejects_private_hardlinked_objects_before_reads(self):
        self.assert_private_object_cache_rejected('hardlink')

    def test_cache_rejects_external_common_directory_before_reads(self):
        self.assert_private_object_cache_rejected('commondir')

    def test_fetch_rejects_private_pack_symlink_before_reads(self):
        self.assert_private_object_cache_rejected('pack-directory', after_fetch=True)

    def test_fetch_rejects_private_loose_symlink_before_reads(self):
        self.assert_private_object_cache_rejected('loose-file', after_fetch=True)

    def test_all_metadata_symlinks_deny_before_any_git_read(self):
        for relative in ('objects/pack', 'objects/ab', 'objects/ab/object', 'refs',
                         'refs/heads', 'refs/heads/linked', 'config', 'HEAD', 'packed-refs',
                         'info', 'info/grafts', 'objects/info/alternates', 'hooks/arbitrary'):
            with self.subTest(path=relative):
                clone = self.root / ('cache-' + relative.replace('/', '-'))
                shutil.copytree(self.fixture, clone)
                self.addCleanup(shutil.rmtree, clone, ignore_errors=True)
                path = clone / '.git' / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
                path.symlink_to(self.root / 'missing-target')
                sources = learn_replay.SourceProjects(None, self.root / 'workspace', 'Owner', 30)
                with patch.object(learn_replay.subprocess, 'run') as git:
                    with self.assertRaisesRegex(c.ColdfixError, 'unsafe public cache'):
                        sources.public_cache(clone, self.entry['repo'])
                    git.assert_not_called()
                shutil.rmtree(clone)

    def test_cache_rejects_grafted_history(self):
        sources = learn_replay.SourceProjects(None, self.root / 'workspace', 'Owner', 30,
                                             admissions={self.entry['repo']: self.entry})
        with patch.object(public_admission, 'identity'), \
                patch.object(learn_replay, 'clone_source', side_effect=self.tracking_transport):
            repo = sources.get(self.failure, (self.before, self.after))
        grafts = repo / '.git/info/grafts'
        grafts.write_text(self.after + '\n')
        with patch.object(public_admission, 'identity'), \
                patch.object(learn_replay, 'clone_source') as transport:
            with self.assertRaisesRegex(c.ColdfixError, 'unsafe public cache'):
                sources.get(self.failure, (self.before, self.after))
            transport.assert_not_called()

    def test_cache_rejects_fetch_into_replacement_namespace(self):
        sources = learn_replay.SourceProjects(None, self.root / 'workspace', 'Owner', 30,
                                             admissions={self.entry['repo']: self.entry})
        with patch.object(public_admission, 'identity'), \
                patch.object(learn_replay, 'clone_source', side_effect=self.tracking_transport):
            repo = sources.get(self.failure, (self.before, self.after))
        self.git('-C', str(repo), 'config', 'remote.origin.fetch', '+refs/heads/*:refs/replace/*')
        with patch.object(public_admission, 'identity'), \
                patch.object(learn_replay, 'clone_source') as transport:
            with self.assertRaisesRegex(c.ColdfixError, 'unsafe public cache'):
                sources.get(self.failure, (self.before, self.after))
            transport.assert_not_called()

    def test_history_acquires_admitted_after_absent_from_advertised_history(self):
        (self.fixture / 'tests').mkdir()
        (self.fixture / 'tests/test_value.py').write_text(
            'import unittest\nclass ValueTests(unittest.TestCase):\n    def test_value(self):\n        pass\n')
        self.git('-C', str(self.fixture), 'add', '.')
        self.git('-C', str(self.fixture), 'commit', '-qm', 'fix with regression')
        after = self.git('-C', str(self.fixture), 'rev-parse', 'HEAD')
        self.entry['after'] = after
        self.git('-C', str(self.fixture), 'update-ref', 'refs/hidden/fix', after)
        self.git('-C', str(self.fixture), 'reset', '--hard', self.before)
        self.git('-C', str(self.fixture), 'config', 'uploadpack.hideRefs', 'refs/hidden')
        self.git('-C', str(self.fixture), 'config', 'uploadpack.allowAnySHA1InWant', 'true')
        self.failure['error'] = json.dumps(dict(type='coldpass', commit=self.before, repository_id=123,
                                               log_tail='FAIL: test_value (test_value.ValueTests)'))
        sources = learn_replay.SourceProjects(None, self.root / 'workspace', 'Owner', 30,
                                             admissions={self.entry['repo']: self.entry})
        fetched = []

        def transport(command, timeout, env):
            command = list(command)
            if command[1] == 'clone':
                command.insert(2, '--no-local')
                command[-2] = str(self.fixture)
                subprocess.run(command, env=env, check=True, capture_output=True, timeout=timeout)
                self.git('-C', command[-1], 'remote', 'set-url', 'origin',
                         'https://github.com/Public/Sample-Project.git')
                self.assertEqual(self.git('-C', command[-1], 'log', '--all', '--format=%H'), self.before)
                self.assertNotEqual(subprocess.run(['git', '-C', command[-1], 'cat-file', '-e', after],
                                                  capture_output=True).returncode, 0)
            else:
                fetched.append(command[-1])
                command[command.index('origin')] = str(self.fixture)
                subprocess.run(command, env=env, check=True, capture_output=True, timeout=timeout)

        with patch.object(public_admission, 'identity'), \
                patch.object(learn_replay, 'clone_source', side_effect=transport):
            with self.assertRaisesRegex(c.ColdfixError, 'missing_upstream_fix'):
                list(learn_replay.history_candidates(self.failure, sources, 30))
            repo = sources.get(self.failure, (self.before, after))
            self.assertEqual(self.git('-C', str(repo), 'rev-parse', after), after)
        self.assertEqual(fetched, ['+' + after + ':refs/coldfix/' + after])

    def test_unsafe_branch_tracking_config_denies(self):
        clone = self.root / 'cache'
        shutil.copytree(self.fixture, clone)
        self.git('-C', str(clone), 'config', '--remove-section', 'user')
        self.git('-C', str(clone), 'remote', 'add', 'origin', 'https://github.com/Public/Sample-Project.git')
        sources = learn_replay.SourceProjects(None, self.root / 'workspace', 'Owner', 30)
        for key, value in (('branch.trunk.remote', '/private/mirror'),
                           ('branch.main.remote', 'https://private.example/repo'),
                           ('branch.master.merge', 'refs/pull/1/head'),
                           ('branch.trunk.merge', 'refs/pull/1/head'),
                           ('branch.trunk.merge', 'refs/heads/../secret'),
                           ('branch.trunk.merge', 'HEAD'),
                           ('branch.bad..name.remote', 'origin'),
                           ('branch.trunk.pushremote', 'origin'),
                           ('branch.trunk.mergeoptions', '--upload-pack=unsafe')):
            with self.subTest(key=key, value=value):
                self.git('-C', str(clone), 'config', key, value)
                with self.assertRaisesRegex(c.ColdfixError, 'unsafe public cache'):
                    sources.public_cache(clone, self.entry['repo'])
                self.git('-C', str(clone), 'config', '--unset-all', key)

    def test_manifest_digest_and_unbounded_revision_deny(self):
        self.manifest()
        path = self.root / public_admission.PATH
        payload = json.loads(path.read_text())
        payload['digest'] = '0' * 64
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(c.ColdfixError, 'digest mismatch'):
            public_admission.load(self.root)
        workspace = self.root / 'workspace'
        workspace.mkdir()
        sources = learn_replay.SourceProjects(None, workspace, 'Owner', 30,
                                             admissions={self.entry['repo']: self.entry})
        with patch.object(public_admission, 'identity'), patch.object(sources, 'mirror') as mirror:
            with self.assertRaisesRegex(c.ColdfixError, 'revisions'):
                sources.get(self.failure, ('unbounded',))
            mirror.assert_not_called()

    def test_identity_change_at_acquisition_denies(self):
        self.manifest()
        with patch.object(public_admission, 'identity', side_effect=[None, c.ColdfixError('renamed')]), \
                patch.object(learn_replay, 'clone_source') as clone:
            with self.assertRaisesRegex(c.ColdfixError, 'renamed'):
                self.daily()
            clone.assert_not_called()

    def test_cache_credential_helper_and_local_alternates_deny(self):
        workspace = self.root / 'workspace'
        clone = workspace / 'public/public/sample-project'
        clone.parent.mkdir(parents=True)
        shutil.copytree(self.fixture, clone)
        self.git('-C', str(clone), 'config', '--remove-section', 'user')
        self.git('-C', str(clone), 'remote', 'add', 'origin', 'https://github.com/Public/Sample-Project.git')
        self.git('-C', str(clone), 'config', 'credential.helper', '!false')
        sources = learn_replay.SourceProjects(None, workspace, 'Owner', 30)
        with self.assertRaisesRegex(c.ColdfixError, 'unsafe public cache'):
            sources.mirror('Public/Sample-Project', [self.before], True)
        self.git('-C', str(clone), 'config', '--remove-section', 'credential')
        (clone / '.git/objects/info/alternates').write_text('/private/objects')
        with self.assertRaisesRegex(c.ColdfixError, 'unsafe public cache'):
            sources.mirror('Public/Sample-Project', [self.before], True)
