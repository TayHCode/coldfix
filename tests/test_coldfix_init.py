import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import coldfix

import test_cli
import test_draft


class InitTests(unittest.TestCase):
    run_cli = test_cli.CliTests.run_cli
    event = test_cli.CliTests.event
    git = test_draft.DraftTests.git

    def setUp(self):
        test_draft.DraftTests.setUp(self)
        self.reply.write_text(json.dumps(self.response))
        self.store = self.cwd / 'init-store'
        self.store.mkdir()
        self.inputs = self.cwd / 'events.jsonl'
        self.inputs.write_text('')

    def init(self, *options, code=0):
        return self.run_cli('--store', str(self.store), 'init', '--repo', str(self.repo), *options, code=code)

    def test_init_paths_and_overwrite(self):
        result = self.init('--input', 'events.jsonl', '--target', 'repo')
        config = json.loads((self.store / 'config.json').read_text())
        self.assertEqual(config, {'repo': str(self.repo), 'input': str(self.inputs), 'target': str(self.repo)})
        self.assertEqual(json.loads(result.stdout), config)
        self.init('--input', 'other', code=2)
        self.assertEqual(json.loads((self.store / 'config.json').read_text()), config)
        self.init('--force', '--input', 'other')
        self.assertEqual(json.loads((self.store / 'config.json').read_text())['input'], str(self.cwd / 'other'))

    def test_init_yes_keeps_an_existing_config(self):
        config = json.loads(self.init('--input', 'events.jsonl').stdout)
        result = self.init('--yes', '--input', 'other')
        self.assertEqual(json.loads(result.stdout), config)
        self.assertIn('kept existing', result.stderr)
        self.assertEqual(json.loads((self.store / 'config.json').read_text()), config)
        (self.store / 'config.json').write_text('{broken')
        self.assertIn('invalid config JSON', self.init('--yes', code=2).stderr)
        self.assertEqual((self.store / 'config.json').read_text(), '{broken')
        self.init('--yes', '--force', '--input', 'other', code=2)
        self.assertEqual((self.store / 'config.json').read_text(), '{broken')

    def test_init_asks_before_overwriting(self):
        config = json.loads(self.init('--input', 'events.jsonl').stdout)
        argv = ['--store', str(self.store), 'init', '--repo', str(self.repo), '--input', str(self.cwd / 'other')]
        for answer, code, expected in (('n', 2, config['input']), ('y', 0, str(self.cwd / 'other'))):
            with self.subTest(answer=answer), patch.object(coldfix, 'ask', return_value=answer):
                self.assertEqual(coldfix.main(argv), code)
                self.assertEqual(json.loads((self.store / 'config.json').read_text())['input'], expected)

    def test_init_runs_doctor_and_reports_it(self):
        result = self.init()
        self.assertIn('coldfix: sandbox: available', result.stderr)
        self.assertIn('coldfix: private_hosts:', result.stderr)

    def test_init_yes_outside_git_needs_repo(self):
        result = self.run_cli('--store', str(self.store), 'init', '--yes', code=2)
        self.assertIn('not inside a Git repository', result.stderr)
        self.assertFalse((self.store / 'config.json').exists())
        result = self.run_cli('--store', str(self.store), 'init', '--yes', '--repo', str(self.repo))
        self.assertEqual(json.loads(result.stdout), {'repo': str(self.repo)})

    def test_init_stops_on_a_failing_doctor_without_writing(self):
        result = subprocess.run([sys.executable, str(test_cli.CLI), '--store', str(self.store), 'init', '--yes'],
                                cwd=self.cwd, capture_output=True, text=True, env=dict(os.environ, PATH=str(self.cwd)))
        self.assertEqual(result.returncode, 2)
        self.assertIn('sandbox unavailable: bwrap is not installed', result.stderr)
        self.assertFalse((self.store / 'config.json').exists())

    def test_init_keeps_release_check_command_name_and_resolves_paths(self):
        self.init('--release-check', 'coldrelease', '--release-check-repo', 'repo')
        config = json.loads((self.store / 'config.json').read_text())
        self.assertEqual((config['release_check'], config['release_check_repo']), ('coldrelease', str(self.repo)))
        self.init('--force', '--release-check', './tools/release_check.py')
        config = json.loads((self.store / 'config.json').read_text())
        self.assertEqual(config['release_check'], str(self.cwd / 'tools' / 'release_check.py'))

    def test_learn_uses_config_and_cli_overrides(self):
        events = [self.event(error='different failure', source_ref=ref) for ref in ('one', 'two')]
        self.inputs.write_text('\n'.join(map(json.dumps, events)))
        self.init('--input', str(self.inputs))
        result = json.loads(self.run_cli('--store', str(self.store), 'learn', code=2).stdout)
        self.assertEqual(result['ingested'], 2)
        empty = self.cwd / 'empty.jsonl'
        empty.write_text(json.dumps(self.event(error='override failure', source_ref='override')))
        result = json.loads(self.run_cli('--store', str(self.store), 'learn', '--input', str(empty), code=2).stdout)
        self.assertEqual(result['ingested'], 1)

    def test_invalid_config_rejected_before_ingest(self):
        for config, key in (({'unknown': 1}, 'unknown'), ({'timeout': 'slow'}, 'timeout')):
            with self.subTest(key=key):
                (self.store / 'config.json').write_text(json.dumps(config))
                result = self.run_cli('--store', str(self.store), 'learn', code=2)
                self.assertIn(key, result.stderr)
                self.assertFalse((self.store / 'groups.json').exists())

    def test_learn_ignores_retired_beacon_config(self):
        self.init('--input', str(self.inputs))
        path = self.store / 'config.json'
        config = json.loads(path.read_text())
        config.update(beacon='/old/beacon', beacon_log_path='/old/log')
        path.write_text(json.dumps(config))
        result = json.loads(self.run_cli('--store', str(self.store), 'learn').stdout)
        self.assertEqual(result['ingested'], 0)

    def test_missing_repo(self):
        result = self.run_cli('--store', str(self.store), 'learn', code=2)
        self.assertIn('learn needs --repo or coldfix init', result.stderr)

    def test_infers_revision_from_review_and_verify_head(self):
        events = [self.event(error=json.dumps({'finding': 'value mismatch', 'head': self.before}),
                             source_ref='head' + str(index), occurred=f'2026-10-2{index}T12:00:00Z')
                  for index in range(2)]
        self.inputs.write_text('\n'.join(map(json.dumps, events)))
        self.init('--input', str(self.inputs))
        result = json.loads(self.run_cli('--store', str(self.store), 'learn', code=2).stdout)
        self.assertEqual(result['skipped_no_revision'], 0)
        self.assertEqual(result['drafted'], 1)
        self.assertIn(self.before, json.loads(self.prompt.read_text())['prompt'])

    def test_infers_newest_failure_revision_and_skips_missing(self):
        events = []
        for error in ('value mismatch', 'other mismatch'):
            for index in range(2):
                metadata = {'message': error}
                if error == 'value mismatch':
                    metadata['head_sha'] = self.before if index else self.after
                events.append(self.event(error=json.dumps(metadata), source_ref=error + str(index),
                                         occurred=f'2026-10-2{index}T12:00:00Z'))
        self.inputs.write_text('\n'.join(map(json.dumps, events)))
        self.init('--input', str(self.inputs))
        result = json.loads(self.run_cli('--store', str(self.store), 'learn', code=2).stdout)
        self.assertEqual(result['skipped_no_revision'], 1)
        self.assertEqual(result['drafted'], 1)
        prompt = json.loads(self.prompt.read_text())['prompt']
        self.assertIn(self.before, prompt)
