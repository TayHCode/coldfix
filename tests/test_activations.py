import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import coldfix as c
import test_measurements


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_measurements.MeasurementTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.cwd / 'target'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / '.git/info').mkdir(exist_ok=True)
        (self.repo / '.git/info/exclude').write_text('.worktrees/\n')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.rule = Path('.coldlocker/rules/lesson-' + self.fixture.lesson['lesson_id'] + '.md')
        (self.repo / self.rule).parent.mkdir(parents=True)
        (self.repo / self.rule).write_text('Delivered rule\n')
        self.register('Delivered rule\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Deliver lesson')
        self.commit = self.git('rev-parse', 'HEAD')
        self.output = self.fixture.cwd / 'private' / 'nested' / 'activations.json'

    def git(self, *args):
        environment = dict(os.environ, GIT_AUTHOR_DATE='2025-01-10T12:00:00+00:00',
                           GIT_COMMITTER_DATE='2025-01-10T12:00:00+00:00')
        return subprocess.check_output(['git', '-C', str(self.repo), *args], env=environment, text=True).strip()

    def ledger(self, state='delivered', target=None, body='Delivered rule\n'):
        lesson = self.fixture.lesson
        target = str(target or self.repo)
        row = dict(lesson_id=lesson['lesson_id'], revision=lesson['id'], state=state, target=target,
                   agentlocker=str(self.repo / 'hub.py'), export=str(self.repo / self.rule.name),
                   export_hashes=[c.digest(body)], delivered_sha256=c.digest(body))
        c.atomic_create(self.fixture.store / 'deliveries.json',
                        c.encoded({c.digest(target + lesson['lesson_id']): row}), replace=True)

    def register(self, body, pending=False):
        (self.repo / '.coldlocker/rules.json').write_text(json.dumps({self.rule.stem:
            dict(lesson=True, pending=pending, hashes=[c.digest(body)])}))
        (self.repo / 'AGENTS.md').write_text('- Read `' + self.rule.as_posix() + '`.\n')

    def test_delivered_commit_is_private_evidence_accepted_by_measure(self):
        self.ledger()
        self.git('rm', '-q', str(self.rule))
        self.git('commit', '-qm', 'Remove lesson')
        (self.repo / self.rule).parent.mkdir(parents=True)
        (self.repo / self.rule).write_text('Delivered again\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Readd lesson')
        result = self.fixture.run_cli('activations', '--output', str(self.output), code=0)
        self.assertNotIn(str(self.repo), result.stdout + result.stderr)
        data = json.loads(self.output.read_text())
        self.assertEqual(data, dict(version=1, activations=[dict(lesson_revision=self.fixture.lesson['id'],
            at=self.git('show', '-s', '--format=%cI', self.commit), commit=self.commit,
            evidence_ref=str(self.repo) + ':' + self.rule.as_posix() + '@' + self.commit)]))
        for path in (self.output.parent.parent, self.output.parent, self.output):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600 if path == self.output else 0o700)
        measured = self.fixture.run_cli('measure', '--input', str(self.output))
        outcome_id, = json.loads(measured.stdout)['outcome_ids']
        outcome = json.loads((self.fixture.store / 'outcomes' / (outcome_id + '.json')).read_text())
        self.assertEqual(outcome['activation'], data['activations'][0])

    def test_withdrawn_missing_rule_and_missing_target_have_no_activation(self):
        for state, target in [('withdrawn', self.repo), ('delivered', self.repo),
                              ('delivered', self.repo / 'missing')]:
            with self.subTest(state=state, target=target):
                self.ledger(state, target)
                if state == 'delivered':
                    (self.repo / self.rule).unlink(missing_ok=True)
                result = self.fixture.run_cli('activations', '--output', str(self.output), code=0)
                self.assertEqual(json.loads(self.output.read_text()), dict(version=1, activations=[]))
                self.assertEqual(json.loads(result.stdout)['skipped'], 1)
                self.output.unlink()

    def deliver_copy(self, repo, body, message):
        (repo / self.rule).parent.mkdir(parents=True, exist_ok=True)
        (repo / self.rule).write_text(body)
        current, self.repo = self.repo, repo
        self.register(body)
        self.git('add', '.')
        self.git('commit', '-qm', message)
        self.repo = current

    def test_merged_worktree_delivery_activates_on_the_repo_it_reached(self):
        worktree = self.repo / '.worktrees' / 'deliver'
        self.git('worktree', 'add', '-q', '-b', 'deliver', str(worktree))
        body = 'Worktree delivery\n'
        (worktree / self.rule).write_text(body)
        main, self.repo = self.repo, worktree
        self.register(body)
        self.git('commit', '-qam', 'Deliver in worktree')
        self.repo = main
        self.git('merge', '-q', '--squash', 'deliver')
        self.git('commit', '-qm', 'Squash-merge delivery')
        merged = self.git('rev-parse', 'HEAD')
        self.git('worktree', 'remove', str(worktree))
        self.ledger(target=worktree, body=body)
        result = self.fixture.run_cli('activations', '--output', str(self.output), code=0)
        self.assertEqual(json.loads(self.output.read_text())['activations'], [])
        self.assertEqual(json.loads(result.stdout)['missing_target'], 1)
        self.output.unlink()
        self.fixture.run_cli('activations', '--output', str(self.output), '--repo', str(self.repo), code=0)
        entry, = json.loads(self.output.read_text())['activations']
        self.assertEqual((entry['commit'], entry['evidence_ref']),
                         (merged, str(self.repo) + ':' + self.rule.as_posix() + '@' + merged))

    def test_repo_without_the_proven_history_cannot_supply_activation(self):
        body = 'Copied rule\n'
        unrelated = self.fixture.cwd / 'unrelated'
        subprocess.check_call(['git', 'init', '-q', str(unrelated)])
        for key, value in (('user.name', 'Test'), ('user.email', 'test@example.invalid')):
            subprocess.check_call(['git', '-C', str(unrelated), 'config', key, value])
        self.deliver_copy(unrelated, body, 'Copy rule')
        self.ledger(target=self.fixture.cwd / 'removed-worktree', body=body)
        result = self.fixture.run_cli('activations', '--output', str(self.output), '--repo', str(unrelated), code=0)
        self.assertEqual(json.loads(self.output.read_text())['activations'], [])
        self.assertEqual(json.loads(result.stdout)['missing_target'], 1)

    def test_unreadable_target_history_blocks_a_later_repo_match(self):
        body = 'Cloned delivery\n'
        clone = self.repo / '.worktrees' / 'clone'
        subprocess.check_call(['git', 'clone', '-q', '--no-local', str(self.repo), str(clone)])
        for key, value in (('user.name', 'Test'), ('user.email', 'test@example.invalid')):
            subprocess.check_call(['git', '-C', str(clone), 'config', key, value])
        self.deliver_copy(clone, body, 'Deliver in clone')
        blob = subprocess.check_output(['git', '-C', str(clone), 'rev-parse', 'HEAD:' + self.rule.as_posix()], text=True).strip()
        (clone / '.git/objects' / blob[:2] / blob[2:]).unlink()
        self.deliver_copy(self.repo, body, 'Merge delivery later')
        self.ledger(target=clone, body=body)
        result = self.fixture.run_cli('activations', '--output', str(self.output), '--repo', str(self.repo), code=0)
        self.assertEqual(json.loads(self.output.read_text())['activations'], [])
        self.assertEqual(json.loads(result.stdout)['unreadable_history'], 1)

    def test_shallow_repo_blocks_a_later_target_activation(self):
        shallow = self.fixture.cwd / 'shallow'
        self.git('clone', '-q', '--depth=1', self.repo.as_uri(), str(shallow))
        target = shallow / '.worktrees' / 'target'
        subprocess.check_call(['git', 'clone', '-q', '--no-local', str(self.repo), str(target)])
        for key, value in (('user.name', 'Test'), ('user.email', 'test@example.invalid')):
            subprocess.check_call(['git', '-C', str(target), 'config', key, value])
        body = 'Target delivery\n'
        self.deliver_copy(target, body, 'Deliver in target')
        self.ledger(target=target, body=body)
        self.fixture.run_cli('activations', '--output', str(self.output), code=0)
        self.assertEqual(len(json.loads(self.output.read_text())['activations']), 1)
        self.output.unlink()
        result = self.fixture.run_cli('activations', '--output', str(self.output), '--repo', str(shallow), code=0)
        self.assertEqual(json.loads(self.output.read_text())['activations'], [])
        self.assertEqual(json.loads(result.stdout)['incomplete_history'], 1)

    def test_symlinked_rule_skips_only_that_delivery(self):
        self.ledger()
        real = self.fixture.cwd / 'elsewhere.md'
        real.write_text('Delivered rule\n')
        (self.repo / self.rule).unlink()
        (self.repo / self.rule).symlink_to(real)
        result = self.fixture.run_cli('activations', '--output', str(self.output), code=0)
        self.assertEqual(json.loads(self.output.read_text())['activations'], [])
        self.assertEqual(json.loads(result.stdout)['missing_commit'], 1)

    def test_revised_delivery_requires_matching_committed_content(self):
        self.fixture.lesson = self.fixture.proven_lesson(previous=self.fixture.lesson)
        (self.repo / self.rule).write_text('New revision\n')
        self.ledger(body='New revision\n')
        self.fixture.run_cli('activations', '--output', str(self.output), code=0)
        self.assertEqual(json.loads(self.output.read_text())['activations'], [])
        self.output.unlink()
        self.register('New revision\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Deliver revised content')
        self.fixture.run_cli('activations', '--output', str(self.output), code=0)
        entry, = json.loads(self.output.read_text())['activations']
        self.assertEqual(entry['commit'], self.git('rev-parse', 'HEAD'))

    def test_activation_waits_for_registration_and_agents_link(self):
        body = 'Later activation\n'
        (self.repo / self.rule).write_text(body)
        self.ledger(body=body)
        for pending, linked in [(False, False), (True, True), (False, True)]:
            self.register(body, pending)
            if not linked:
                (self.repo / 'AGENTS.md').write_text('No rules yet\n')
            self.git('add', '.')
            self.git('commit', '-qm', 'Update registration')
            self.fixture.run_cli('activations', '--output', str(self.output), code=0)
            entries = json.loads(self.output.read_text())['activations']
            if pending or not linked:
                self.assertEqual(entries, [])
            else:
                self.assertEqual(entries[0]['commit'], self.git('rev-parse', 'HEAD'))
            self.output.unlink()

    def test_shallow_target_cannot_turn_a_boundary_into_activation(self):
        (self.repo / 'unrelated').write_text('Later commit\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Unrelated later change')
        shallow = self.fixture.cwd / 'shallow'
        self.git('clone', '-q', '--depth=1', self.repo.as_uri(), str(shallow))
        self.ledger(target=shallow)
        result = self.fixture.run_cli('activations', '--output', str(self.output), code=0)
        self.assertEqual(json.loads(self.output.read_text())['activations'], [])
        self.assertEqual(json.loads(result.stdout)['incomplete_history'], 1)

    def test_export_waits_for_withdrawal_and_reloads_the_ledger(self):
        self.ledger()
        runner = ("import fcntl, runpy, sys\noriginal = fcntl.flock\n"
                  "def notified(fd, operation):\n"
                  "    if operation == fcntl.LOCK_EX: print('waiting', flush=True)\n"
                  "    return original(fd, operation)\n"
                  "fcntl.flock = notified\nsys.argv = sys.argv[1:]\n"
                  "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        command = [sys.executable, '-c', runner, str(test_measurements.CLI), '--store', str(self.fixture.store),
                   'activations', '--output', str(self.output)]
        with (self.fixture.store / '.lessons.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
                try:
                    self.assertEqual(child.stdout.readline(), 'waiting\n')
                    self.ledger('withdrawn')
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout)['not_delivered'], 1)
        self.assertEqual(json.loads(self.output.read_text())['activations'], [])

    def test_missing_historical_blob_cannot_shift_activation_to_a_later_commit(self):
        body = 'Later committed revision\n'
        (self.repo / self.rule).write_text(body)
        self.register(body)
        self.git('add', '.')
        self.git('commit', '-qm', 'Deliver later revision')
        self.ledger(body=body)
        blob = self.git('rev-parse', self.commit + ':' + self.rule.as_posix())
        (self.repo / '.git/objects' / blob[:2] / blob[2:]).unlink()
        result = self.fixture.run_cli('activations', '--output', str(self.output), code=0)
        self.assertEqual(json.loads(self.output.read_text())['activations'], [])
        self.assertEqual(json.loads(result.stdout)['skipped'], 1)
