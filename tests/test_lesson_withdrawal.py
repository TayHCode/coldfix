import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_cli
import test_lesson_delivery

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import coldfix
import lesson_delivery


class Fixture:
    setUp = test_cli.CliTests.setUp
    run_cli = test_cli.CliTests.run_cli
    event = test_cli.CliTests.event
    public_inputs = test_cli.CliTests.public_inputs
    prepare = test_lesson_delivery.AutoDeliveryTests.prepare

    def deliver(self):
        args, inputs, fixes, fix, repo, signature = self.prepare()
        self.public_inputs('pass')
        self.run_cli(*args, *inputs, stdin='')
        self.repo = repo
        rule, = (repo / '.coldlocker/rules').glob('lesson-*.md')
        self.rule = rule
        self.lesson = rule.stem
        digest = hashlib.sha256(rule.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
        (repo / '.coldlocker/rules.json').write_text(json.dumps({self.lesson: dict(lesson=True, pending=False, hashes=[digest])}))

    def break_witness(self):
        (self.repo / 'value').write_text('bad')

    def delivery_states(self):
        rows = json.loads((self.store / 'deliveries.json').read_text()).values()
        return sorted(row['state'] for row in rows)

    def add_delivery(self, target, replace=False):
        path = self.store / 'deliveries.json'
        records = json.loads(path.read_text())
        row = dict(next(iter(records.values())), target=str(target))
        if replace:
            records = {}
        records[coldfix.digest(str(coldfix.ordinary_path(target)) + row['lesson_id'])] = row
        path.write_text(json.dumps(records))

    def recheck(self, *extra, code=0):
        result = self.run_cli('recheck', '--repo', str(self.repo), *extra, code=code)
        return json.loads(result.stdout)

    def row(self, result, **reason):
        return dict(lesson=self.lesson.removeprefix('lesson-'), target=str(self.repo), result=result, **reason)

    def lesson_file(self, folder, name, verification):
        folder.mkdir(exist_ok=True)
        number = name.split('-')[0]
        (folder / name).write_text(f'---\nid: {number}\nslug: s\ntitle: t\nscope: global\nlevel: doc\nstatus: active\n'
                                   f'repos:\nverification: {verification}\n---\n# {number} t\n')

class WithdrawalTests(Fixture, unittest.TestCase):
    def test_recheck_leaves_a_passing_lesson_alone(self):
        self.deliver()
        before = self.rule.read_bytes()
        counts = self.recheck()
        self.assertEqual(counts, dict(delivered=1, passed=1, withdrawn=0, regression=0, retired=0, unchecked=0, gone=0, lessons=[self.row('passed', reason='witness passed')]))
        self.assertEqual(self.rule.read_bytes(), before)
        self.assertEqual(self.delivery_states(), ['delivered'])

    def test_recheck_withdraws_a_lesson_whose_witness_fails(self):
        self.deliver()
        self.break_witness()
        counts = self.recheck(code=1)
        self.assertEqual(counts, dict(delivered=1, passed=0, withdrawn=1, regression=0, retired=0, unchecked=0, gone=0, lessons=[self.row('withdrawn')]))
        self.assertFalse(self.rule.exists())
        self.assertEqual(self.delivery_states(), ['withdrawn'])
        self.assertEqual(self.recheck(), dict(delivered=0, passed=0, withdrawn=0, regression=0, retired=0, unchecked=0, gone=0, lessons=[]))

    def note(self, folder):
        text = (folder / (self.lesson + '-' + self.repo.name + '.md')).read_text()
        front = text.split('---\n')[1]
        return text, dict(line.split(': ', 1) for line in front.splitlines() if ': ' in line and not line.startswith(' '))

    def test_recheck_notes_flip_to_withdrawn_when_the_witness_fails(self):
        self.deliver()
        folder = self.store.parent / 'vault-notes'
        self.recheck('--notes', str(folder))
        text, front = self.note(folder)
        self.assertEqual(front['status'], 'active')
        self.assertRegex(front['last_recheck'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(front['scope'], '"repository-specific"')
        self.assertIn('  - "coldfix:replay:', text)
        self.break_witness()
        self.recheck('--notes', str(folder), code=1)
        self.assertEqual(self.note(folder)[1]['status'], 'withdrawn')

    def test_recheck_notes_catch_up_with_a_withdrawal_made_without_notes(self):
        self.deliver()
        folder = self.store.parent / 'vault-notes'
        self.recheck('--notes', str(folder))
        self.break_witness()
        self.recheck(code=1)
        self.assertEqual(self.note(folder)[1]['status'], 'active')
        self.recheck('--notes', str(folder))
        self.assertEqual(self.note(folder)[1]['status'], 'withdrawn')

    def test_recheck_notes_keep_the_last_recheck_when_no_witness_runs(self):
        self.deliver()
        folder = self.store.parent / 'vault-notes'
        self.recheck('--notes', str(folder))
        path = folder / (self.lesson + '-' + self.repo.name + '.md')
        text, front = self.note(folder)
        path.write_text(text.replace('last_recheck: ' + front['last_recheck'], 'last_recheck: 2000-01-01T00:00:00Z'))
        self.add_delivery(self.store.parent / 'elsewhere', replace=True)
        self.recheck('--notes', str(folder))
        front = self.note(folder)[1]
        self.assertEqual((front['status'], front['last_recheck']), ('active', '2000-01-01T00:00:00Z'))

    def test_recheck_withdraws_only_deliveries_to_its_own_repository(self):
        self.deliver()
        other = self.cwd / 'other-checkout'
        other.mkdir()
        subprocess.run(['git', '-C', str(other), 'init', '-q'], check=True)
        self.add_delivery(other)
        self.break_witness()
        self.recheck(code=1)
        rows = json.loads((self.store / 'deliveries.json').read_text()).values()
        self.assertEqual(sorted((row['target'] == str(other), row['state']) for row in rows),
                         [(False, 'withdrawn'), (True, 'delivered')])

    def test_recheck_ignores_a_lesson_delivered_to_a_subdirectory(self):
        self.deliver()
        sub = self.repo / 'sub'
        sub.mkdir()
        self.add_delivery(sub, replace=True)
        self.break_witness()
        self.assertEqual(self.recheck(), dict(delivered=0, passed=0, withdrawn=0, regression=0, retired=0, unchecked=0, gone=0, lessons=[]))
        self.assertEqual(self.delivery_states(), ['delivered'])

    def test_recheck_without_deliveries_reports_zero_counts(self):
        self.deliver()
        (self.store / 'deliveries.json').unlink()
        (self.repo / '.coldlocker/rules.json').unlink()
        self.assertEqual(self.recheck(), dict(delivered=0, passed=0, withdrawn=0, regression=0, retired=0, unchecked=0, gone=0, lessons=[]))

    def test_recheck_runs_only_delivered_witnesses_and_leaves_the_image_alone(self):
        self.deliver()
        import coldrecall
        ran = []
        original = coldrecall.run_witness
        def spy(repo, fact, head, timeout):
            ran.append(fact['lesson_id'])
            return original(repo, fact, head, timeout)
        coldrecall.run_witness = spy
        self.addCleanup(setattr, coldrecall, 'run_witness', original)
        delivered = {row['lesson_id'] for row in json.loads((self.store / 'deliveries.json').read_text()).values()}
        args = argparse.Namespace(repo=str(self.repo), timeout=30.0, notes=None)
        _, store = coldfix.load_store(self.store)
        lesson_delivery.recheck(args, self.store, store)
        self.assertEqual(ran, sorted(delivered))
        self.assertFalse((self.repo / coldrecall.DIRECTORY).exists())

    def test_recheck_reruns_a_delivery_whose_image_fact_was_retired(self):
        self.deliver()
        import coldrecall, sqlite3
        coldrecall.compile_image(self.repo)
        image = self.repo / coldrecall.DIRECTORY / coldrecall.IMAGE
        with sqlite3.connect(image) as database:
            for ref, body in database.execute('SELECT ref, body FROM facts').fetchall():
                database.execute('UPDATE facts SET body = ? WHERE ref = ?',
                                 (json.dumps(dict(json.loads(body), state='retired', reason='test')), ref))
        self.break_witness()
        self.assertEqual(self.recheck(code=1), dict(delivered=1, passed=0, withdrawn=1, regression=0, retired=0, unchecked=0, gone=0,
                                                    lessons=[self.row('withdrawn')]))

    def test_recheck_reports_a_different_registered_revision_unchecked(self):
        self.deliver()
        import coldrecall
        original = coldrecall.read_sources
        def other_revision(repo):
            found, closed, skipped = original(repo)
            moved = {ref.rsplit(':', 1)[0] + ':' + 'f' * 32: fact for ref, fact in found.items()}
            return {ref: dict(fact, ref=ref) for ref, fact in moved.items()}, closed, skipped
        coldrecall.read_sources = other_revision
        self.addCleanup(setattr, coldrecall, 'read_sources', original)
        args = argparse.Namespace(repo=str(self.repo), timeout=30.0, notes=None)
        _, store = coldfix.load_store(self.store)
        self.assertEqual(lesson_delivery.recheck(args, self.store, store),
                         dict(delivered=1, passed=0, withdrawn=0, regression=0, retired=0, unchecked=1, gone=0, _exit=2,
                              lessons=[self.row('unchecked', reason='rule not registered')]))

    def test_recheck_retries_a_withdrawal_whose_catalog_step_failed(self):
        self.deliver()
        self.break_witness()
        path = self.store / 'deliveries.json'
        original = path.read_text()
        records = json.loads(original)
        hub = next(iter(records.values()))['agentlocker']
        failing = self.cwd / 'failing_hub.py'
        failing.write_text('import runpy, sys\nif sys.argv[1:2] == ["remove"]:\n    sys.exit(1)\n'
                           'sys.argv[0] = %r\nrunpy.run_path(%r, run_name="__main__")\n' % (hub, hub))
        path.write_text(json.dumps({key: dict(row, agentlocker=str(failing)) for key, row in records.items()}))
        self.assertEqual(self.recheck(code=2)['withdrawn'], 0)
        self.assertTrue(self.rule.exists())
        path.write_text(original)
        self.assertEqual(self.recheck(code=1)['withdrawn'], 1)
        self.assertFalse(self.rule.exists())

    def test_recheck_finishes_an_interrupted_withdrawal_whose_rule_is_no_longer_registered(self):
        self.deliver()
        path = self.store / 'deliveries.json'
        path.write_text(json.dumps({key: dict(row, state='withdrawing') for key, row in json.loads(path.read_text()).items()}))
        registry = self.repo / '.coldlocker/rules.json'
        registry.write_text(json.dumps({self.lesson: dict(json.loads(registry.read_text())[self.lesson], pending=True)}))
        self.assertEqual(self.recheck(code=1)['withdrawn'], 1)
        self.assertEqual(self.delivery_states(), ['withdrawn'])
        self.assertFalse(self.rule.exists())

    def interrupt_delivery(self):
        path = self.store / 'deliveries.json'
        path.write_text(json.dumps({key: dict(row, state='pending') for key, row in json.loads(path.read_text()).items()}))

    def test_recheck_confirms_an_install_interrupted_before_its_delivered_record_and_runs_its_witness(self):
        self.deliver()
        self.interrupt_delivery()
        self.break_witness()
        self.assertEqual(self.recheck(code=1), dict(delivered=1, passed=0, withdrawn=1, regression=0, retired=0, unchecked=0, gone=0,
                                                    lessons=[self.row('withdrawn')]))
        self.assertEqual(self.delivery_states(), ['withdrawn'])
        self.assertFalse(self.rule.exists())

    def test_recheck_reports_an_unconfirmed_delivery_incomplete(self):
        self.deliver()
        self.interrupt_delivery()
        self.rule.write_text(self.rule.read_text() + '\nPartly written.\n')
        self.assertEqual(self.recheck(code=2), dict(delivered=1, passed=0, withdrawn=0, regression=0, retired=0, unchecked=1, gone=0,
                                                    lessons=[self.row('unchecked', reason='delivery unconfirmed')]))
        self.assertEqual(self.delivery_states(), ['pending'])

    def test_recheck_passes_its_timeout_to_the_witness_run(self):
        self.deliver()
        calls = []
        import coldrecall
        original = coldrecall.run_witness
        def spy(repo, fact, head, timeout):
            calls.append(timeout)
            return original(repo, fact, head, timeout)
        coldrecall.run_witness = spy
        self.addCleanup(setattr, coldrecall, 'run_witness', original)
        args = argparse.Namespace(repo=str(self.repo), timeout=7.0, notes=None)
        _, store = coldfix.load_store(self.store)
        lesson_delivery.recheck(args, self.store, store)
        self.assertEqual(set(calls), {7.0})

    def test_recheck_does_not_trust_a_held_lesson_and_reports_it_unchecked(self):
        self.deliver()
        self.assertEqual(self.recheck()['passed'], 1)
        self.rule.write_text(self.rule.read_text() + '\nEdited after registration.\n')
        counts = self.recheck(code=2)
        self.assertEqual(counts, dict(delivered=1, passed=0, withdrawn=0, regression=0, retired=0, unchecked=1, gone=0,
                                      lessons=[self.row('unchecked', reason='rule not registered')]))

    def test_recheck_outside_a_repository_reports_an_error(self):
        self.deliver()
        elsewhere = self.cwd / 'not-a-repository'
        elsewhere.mkdir()
        self.run_cli('recheck', '--repo', str(elsewhere), code=2)

    def test_recheck_without_a_repo_covers_every_target_and_the_lessons_folder(self):
        self.deliver()
        agents = self.repo / 'AGENTS.md'
        self.assertIn(self.rule.name, agents.read_text())
        folder = self.cwd / 'lessons'
        self.lesson_file(folder, 'L1-docs-sync.md', 'proven (PASS; cited cases only)')
        self.lesson_file(folder, 'L43-use-the-alias.md', 'judged')
        self.break_witness()
        result = json.loads(self.run_cli('recheck', '--lessons', str(folder), code=2).stdout)
        self.assertEqual(result, dict(delivered=3, passed=0, withdrawn=1, regression=0, retired=0, unchecked=2, gone=0, lessons=[
            self.row('withdrawn'), dict(lesson='L1', result='unchecked', reason='no witness'),
            dict(lesson='L43', result='unchecked', reason='judged: stored messages missing')]))
        self.assertFalse(self.rule.exists())
        self.assertNotIn(self.rule.name, agents.read_text())

    def test_recheck_without_a_repo_reports_an_unreadable_target_unchecked(self):
        self.deliver()
        gone = self.cwd / 'removed-checkout'
        gone.mkdir()
        self.add_delivery(gone, replace=True)
        result = json.loads(self.run_cli('recheck', code=2).stdout)
        self.assertEqual(result['lessons'], [dict(self.row('unchecked', reason='repository or rule registry unreadable'),
                                                  target=str(gone))])
        self.assertEqual(self.delivery_states(), ['delivered'])
        gone.rmdir()
        result = json.loads(self.run_cli('recheck', code=2).stdout)
        self.assertEqual((result['gone'], result['lessons']), (1, [dict(
            self.row('unchecked', reason='repository gone'), target=str(gone), gone=[str(gone)])]))
        self.assertEqual(self.delivery_states(), ['delivered'])
