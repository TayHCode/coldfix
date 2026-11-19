import copy
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
import unittest
import zipfile
from unittest.mock import patch

import coldfix as c
import coldpass_inputs as inputs
import learn_candidates
import learn_daily
import learn_replay
import test_coldpass_inputs as fixtures


def wheel_bytes(content):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('package/data.bin', content)
    return stream.getvalue()


class ColdpassDailyTests(unittest.TestCase):
    setUp = fixtures.ColdpassInputTests.setUp
    entry = fixtures.ColdpassInputTests.entry
    write = fixtures.ColdpassInputTests.write
    cli = fixtures.ColdpassInputTests.cli
    stored = fixtures.ColdpassInputTests.stored

    def test_daily_selects_coldpass_pair_by_run_and_commit(self):
        inputs.import_document(self.store, dict(schema_version=1, entries=[self.entry()]))
        pairs = learn_daily.fix_pairs(self.store, coldpass_inputs=True)
        pair = next(row for row in pairs if row.get('type') == 'coldpass')
        wrong = []
        for key, value in [('run_id', 'other'), ('failure_id', '0' * 32),
                           ('source_ref', 'other'), ('repository_id', 999),
                           ('failure_class', 'missing-file'), ('before', 'e' * 40),
                           ('repo', 'other/project'), ('type', 'ci')]:
            wrong.append(dict(pair, **{key: value}))
        store = c.load_store(self.store)[1]
        selected = list(learn_replay.candidates(store, dict(evidence=[self.failure['id']]), wrong + [pair]))
        self.assertEqual(selected, [(self.failure, pair)])
        generic = dict(repo=pair['repo'], before=pair['before'], after=pair['after'])
        self.assertEqual(list(learn_replay.candidates(store, dict(evidence=[self.failure['id']]), [generic])), [])
        row = learn_candidates.candidates(store, pairs=[pair])[0]
        self.assertTrue(row['readiness']['ready'])
        missing = learn_candidates.candidates(store)[0]
        self.assertFalse(missing['readiness']['ready'])
        self.assertEqual(missing['readiness']['evidence'][self.failure['id']], ['missing_reproducer_or_witness', 'missing_upstream_fix'])


class ColdpassFlowTests(unittest.TestCase):
    setUp = fixtures.ColdpassInputTests.setUp
    entry = fixtures.ColdpassInputTests.entry
    write = fixtures.ColdpassInputTests.write
    cli = fixtures.ColdpassInputTests.cli
    stored = fixtures.ColdpassInputTests.stored

    def import_pair(self, *, check=None, revise=False):
        entry = self.entry()
        if check:
            entry['witness']['check'] = check
            entry['witness']['check_sha256'] = c.digest(check)
        inputs.import_document(self.store, dict(schema_version=1, entries=[entry]), revise=revise)
        return entry

    def ask(self, model, prompt, shape, work):
        self.calls.append(model['model'])
        data = json.loads(prompt.split('\n', 1)[1])
        if shape is learn_daily.DRAFTS:
            self.assertIn('witness', data[0])
            return dict(drafts=[dict(fingerprint=row['fingerprint'], kind='test', cause='c', prevention='p', title='Check package imports',
                                    rule='Test imports through the package.', check_command='ignored') for row in data])
        self.assertIn('check_script', data['drafts'][0])
        return dict(reviews=[dict(fingerprint=row['fingerprint'], verdict=self.verdict, reason='Assertion checks the diagnostic.')
                             for row in data['drafts']])

    def daily(self, day='2026-11-10'):
        self.calls = []
        with patch.dict('os.environ', COLDFIX_OWNER='example'), \
                patch.object(learn_replay, 'source_repo', return_value=self.repo):
            return learn_daily.run(self.store, day, route=lambda role: dict(provider='command', model=role), ask=self.ask)

    def test_missing_witness_is_reported_without_model_calls(self):
        import learn_report
        from datetime import datetime, timezone
        self.verdict = 'PASS'
        result = self.daily()
        self.assertEqual(result['calls'], 0)
        report = learn_report.report(self.store, datetime.now(timezone.utc))
        self.assertFalse(report['coldpass_readiness'][0]['ready'])
        self.assertEqual(report['readiness_reasons']['missing_input'],
                         dict(missing_reproducer_or_witness=1, missing_upstream_fix=1))

    def test_revised_witness_is_screened_and_old_rejection_is_preserved(self):
        self.import_pair()
        self.verdict = 'FAIL'
        self.assertEqual(self.daily()['passed'], 0)
        old = json.loads((self.store / learn_daily.LEDGER).read_text())['drafts']
        self.assertEqual(self.daily('2026-11-11')['calls'], 0)
        check = self.entry()['witness']['check'].replace('self.assertEqual(1, 1)', 'self.assertEqual(2, 2)')
        self.import_pair(check=check, revise=True)
        self.verdict = 'PASS'
        self.assertEqual(self.daily('2026-11-12')['passed'], 1)
        ledger = json.loads((self.store / learn_daily.LEDGER).read_text())
        key = next(iter(old))
        self.assertEqual(ledger['revisions'][key][0], dict(old[key], revision=old[key].get('revision', 0)))
        self.assertEqual([row['verdict'] for row in ledger['revisions'][key]], ['FAIL', 'PASS'])
        self.assertEqual(ledger['drafts'][key]['revision'], 2)
        self.assertEqual(len(list((self.store / inputs.HISTORY).glob('*.json'))), 2)
        self.assertEqual(self.daily('2026-11-13')['calls'], 0)

    def test_rejected_revision_keeps_active_draft_and_is_not_retried(self):
        self.import_pair()
        self.verdict = 'PASS'
        self.assertEqual(self.daily()['passed'], 1)
        old = json.loads((self.store / learn_daily.LEDGER).read_text())['drafts']
        check = self.entry()['witness']['check'].replace('self.assertEqual(1, 1)', 'self.assertEqual(2, 2)')
        self.import_pair(check=check, revise=True)
        self.verdict = 'FAIL'
        self.assertEqual(self.daily('2026-11-11')['passed'], 0)
        ledger = json.loads((self.store / learn_daily.LEDGER).read_text())
        self.assertEqual(ledger['drafts'], old)
        self.assertEqual(self.daily('2026-11-12')['calls'], 0)

    def test_unrelated_history_same_tree_and_same_revision_never_spend(self):
        self.import_pair()
        for code in ('same_revision_not_fix', 'same_tree_not_fix', 'unrelated_fix_history', 'fix_patch_unverified'):
            with self.subTest(code=code), patch.object(inputs, 'upstream_history', side_effect=c.ColdfixError('Coldpass replay input: ' + code)):
                self.assertEqual(self.daily()['calls'], 0)
        reasons = []
        store = c.load_store(self.store)[1]
        self.assertEqual(list(learn_replay.replay_candidates(store, dict(evidence=[self.failure['id']]), [], None, 30, reasons)), [])
        self.assertEqual(reasons, [self.failure['id'] + ': missing_upstream_fix'])

    def test_real_witness_runs_three_sides_and_tampered_screen_rejects(self):
        import coldpass_replay
        import replay_isolation
        if replay_isolation.unavailable():
            self.skipTest('requires the existing replay sandbox')
        check = ('import unittest\nimport behavior\nclass Import(unittest.TestCase):\n'
                 '    def test_sibling(self):\n        self.assertEqual(behavior.value, 1)\n')
        self.import_pair(check=check)
        target = self.root / 'project'
        self.repo.rename(target)
        self.repo = target
        subprocess.run(['git', '-C', str(self.repo), 'remote', 'add', 'origin',
                        'https://github.com/example/project.git'], check=True)
        self.verdict, self.calls = 'PASS', []
        with patch.dict('os.environ', COLDFIX_OWNER='example'):
            result = learn_daily.run(self.store, '2026-11-10',
                route=lambda role: dict(provider='command', model=role), ask=self.ask, projects=self.root)
        self.assertEqual(result['passed'], 1)
        ledger = json.loads((self.store / learn_daily.LEDGER).read_text())
        draft = next(iter(ledger['drafts'].values()))
        pairs = learn_daily.fix_pairs(self.store, coldpass_inputs=True)
        sources = learn_replay.SourceProjects(self.root, self.root, 'example', 60)
        coldpass_replay.preflight(self.store, c.load_store(self.store)[1], pairs, sources)
        pair = next(row for row in pairs if row.get('type') == 'coldpass')
        bad = copy.deepcopy(draft)
        bad['coldpass_screen']['check_sha256'] = '0' * 64
        with self.assertRaisesRegex(c.ColdfixError, 'screen_rejected'):
            learn_replay.coldpass_proof(self.store, c.load_store(self.store)[1], bad, self.failure, pair, sources, 60)
        result = learn_replay.run_in_workspace(self.store, ledger, sources, learn_replay.PAIRS, 60, history=False)
        self.assertEqual(result['proven'], 1)
        proof = next(iter(ledger['drafts'].values()))['replay']['proof']
        store = c.load_store(self.store)[1]
        receipt = store['receipts'][proof['receipt_id']]
        self.assertEqual([receipt[side]['status'] for side in ('before', 'after', 'reversed')], ['FAIL', 'PASS', 'FAIL'])
        self.assertEqual(store['lessons'][proof['id']]['evidence'], [self.failure['id']])
        self.assertEqual(store['failures'][self.failure['id']]['error'], self.failure['error'])

    def test_daily_cli_proves_one_coldpass_lesson_once(self):
        self.daily_cli_proves_once(local_checkouts=True)

    def test_daily_cli_without_checkouts_keeps_the_proof(self):
        self.daily_cli_proves_once(local_checkouts=False)
        state = next(iter(json.loads((self.store / learn_daily.LEDGER).read_text())['drafts'].values()))['replay']
        self.assertEqual(state['status'], 'proven')
        self.assertNotIn('delivery_reason', state)

    def daily_cli_proves_once(self, local_checkouts):
        import replay_isolation
        if replay_isolation.unavailable():
            self.skipTest('requires the existing replay sandbox')
        check = ('import unittest\nimport behavior\nclass Import(unittest.TestCase):\n'
                 '    def test_sibling(self):\n        self.assertEqual(behavior.value, 1)\n')
        self.import_pair(check=check)
        target = self.root / 'project'
        self.repo.rename(target)
        subprocess.run(['git', '-C', str(target), 'remote', 'add', 'origin',
                        'https://github.com/example/project.git'], check=True)
        seen, fake = self.root / 'seen.jsonl', self.root / 'fake-model'
        fake.write_text('#!' + sys.executable + '\n'
                        'import json, sys\n'
                        'role, prompt = sys.argv[1], sys.stdin.read()\n'
                        'body = json.loads(prompt[prompt.index(chr(10)) + 1:])\n'
                        'open(' + repr(str(seen)) + ", 'a').write(role + chr(10))\n"
                        "if role == 'drafter':\n"
                        "    print(json.dumps(dict(drafts=[dict(fingerprint=row['fingerprint'], kind='test', cause='c', prevention='p', title='Import siblings via the package',\n"
                        "        rule='Import sibling modules through the package.', check_command='ignored') for row in body])))\n"
                        'else:\n'
                        "    print(json.dumps(dict(reviews=[dict(fingerprint=row['fingerprint'], verdict='PASS', reason='checks the import')\n"
                        "        for row in body['drafts']])))\n")
        fake.chmod(0o700)
        environment = {key: value for key, value in learn_daily.os.environ.items() if key != 'COLDFIX_CHECKOUTS'}
        environment.update(COLDFIX_OWNER='example' if local_checkouts else 'operator', COLDFIX_MODEL_CMD=str(fake))
        identity = patch('public_admission.identity')
        identity.start()
        self.addCleanup(identity.stop)
        if not local_checkouts:
            entries = [dict(repo='example/project', repository_id=123, before=self.before, after=self.after)]
            body = dict(version=1, entries=entries)
            (self.store / 'public-replay-admissions.json').write_text(
                c.encoded(dict(body, digest=hashlib.sha256(c.encoded(body).encode()).hexdigest())))
        argv = ['--store', str(self.store)] + (['--projects', str(self.root)] if local_checkouts else [])
        clone_source = learn_replay.clone_source
        def clone_local(command, timeout, env):
            url = 'https://github.com/example/project.git'
            count = int(env.get('GIT_CONFIG_COUNT', '0'))
            env = dict(env, GIT_CONFIG_COUNT=str(count + 1), **{'GIT_CONFIG_KEY_%d' % count: 'url.%s.insteadOf' % target.as_uri(),
                                                                 'GIT_CONFIG_VALUE_%d' % count: url})
            clone_source(command, timeout, env)
        summaries = []
        for _ in range(2):
            output = io.StringIO()
            with patch.dict('os.environ', environment, clear=True), patch.object(c, 'on_replay_host', return_value=True), \
                    patch('learn_replay.clone_source', side_effect=clone_local), \
                    patch('codex_review_source.resume', return_value=({'counts': {}}, self.store)), \
                    patch('learn_replay.transcript_run', return_value={}), \
                    patch('sys.stdout', output), patch('sys.stderr', io.StringIO()):
                code = learn_daily.main(argv)
            summaries.append(json.loads(output.getvalue()))
            self.assertEqual((code, summaries[-1].get('error')), (0, None))
        self.assertEqual([summary['replay']['rejected'] for summary in summaries], [0, 0])
        self.assertEqual(seen.read_text().splitlines(), ['drafter', 'reviewer'])
        self.assertEqual((summaries[0]['passed'], summaries[0]['replay']['proven']), (1, 1))
        self.assertEqual((summaries[1]['calls'], summaries[1]['replay']['proven']), (0, 0))
        store = c.load_store(self.store)[1]
        self.assertEqual(len(store['receipts']), 1)
        receipt = next(iter(store['receipts'].values()))
        self.assertEqual([receipt[side]['status'] for side in ('before', 'after', 'reversed')], ['FAIL', 'PASS', 'FAIL'])
        lessons = [row for row in store['lessons'].values() if row['admission']['kind'] == 'verified_coldpass_incident']
        self.assertEqual({row['lesson_id'] for row in lessons}, {store['lessons'][receipt['lesson_revision']]['lesson_id']})
        proven = [row for row in lessons if (row['status'], row['verification']) == ('active', 'proven')]
        self.assertEqual([(row['id'], row['evidence']) for row in proven], [(receipt['lesson_revision'], [self.failure['id']])])


class ColdpassBoundaryTests(unittest.TestCase):
    setUp = fixtures.ColdpassInputTests.setUp
    entry = fixtures.ColdpassInputTests.entry
    write = fixtures.ColdpassInputTests.write
    cli = fixtures.ColdpassInputTests.cli
    stored = fixtures.ColdpassInputTests.stored
    import_pair = ColdpassFlowTests.import_pair
    daily = ColdpassFlowTests.daily
    ask = ColdpassFlowTests.ask

    def test_revision_reasons_are_specific_and_safe(self):
        import coldpass_replay
        for fields, expected in [({}, 'missing_revision'), ({'commit': 'not-a-sha'}, 'invalid_revision'),
                                 ({'commit': 'a' * 40, 'sha': 'b' * 40}, 'revision_conflict')]:
            with self.subTest(expected=expected):
                failure = dict(self.failure, error=json.dumps(dict(type='coldpass', **fields)))
                self.assertEqual(coldpass_replay.blockers(failure, [])[0], expected)
        codes = list(coldpass_replay.REASONS)
        self.assertNotIn('ours', learn_replay.reason_buckets(codes))
        self.assertTrue(learn_replay.terminal(['missing_upstream_fix']))
        self.assertFalse(learn_replay.terminal(['replay_incomplete']))

    def test_setup_failure_is_incomplete_and_never_an_assertion_failure(self):
        import replay_isolation
        if replay_isolation.unavailable():
            self.skipTest('requires the existing replay sandbox')
        entry = self.entry()
        setup = self.root / 'setup.py'
        setup.write_text('raise RuntimeError("setup failed")\n')
        entry['witness']['setup'] = [dict(path='setup.py', version='1', sha256=c.digest(setup.read_text()))]
        inputs.import_document(self.store, dict(schema_version=1, entries=[entry]), artifacts=self.root)
        self.verdict = 'PASS'
        self.assertEqual(self.daily()['passed'], 1)
        ledger = json.loads((self.store / learn_daily.LEDGER).read_text())
        sources = learn_replay.SourceProjects(self.root / 'checkouts', self.root, 'example', 30)
        with patch.object(learn_replay, 'source_repo', return_value=self.repo):
            result = learn_replay.run_in_workspace(self.store, ledger, sources, learn_replay.PAIRS, 30, history=False)
        self.assertEqual(result['proven'], 0)
        store = c.load_store(self.store)[1]
        receipt = next(iter(store['receipts'].values()))
        self.assertEqual([receipt[side]['status'] for side in ('before', 'after', 'reversed')], ['INCOMPLETE'] * 3)
        self.assertTrue(all(lesson['verification'] == 'unproven' for lesson in store['lessons'].values()))

    def test_missing_screen_cannot_admit(self):
        self.import_pair()
        self.verdict = 'PASS'
        self.daily()
        ledger = json.loads((self.store / learn_daily.LEDGER).read_text())
        draft = next(iter(ledger['drafts'].values()))
        draft.pop('coldpass_screen')
        pair = next(pair for pair in learn_daily.fix_pairs(self.store, coldpass_inputs=True) if pair.get('type') == 'coldpass')
        with self.assertRaisesRegex(c.ColdfixError, 'screen_rejected'):
            learn_replay.coldpass_proof(self.store, c.load_store(self.store)[1], draft, self.failure, pair, None, 30)

    def test_setup_privacy_is_screened_even_when_its_bytes_are_embedded(self):
        import coldpass_replay
        entry = self.entry()
        setup = self.root / 'setup.py'
        setup.write_text('endpoint = "https://secret.internal"\n')
        entry['witness']['setup'] = [dict(path='setup.py', version='1', sha256=c.digest(setup.read_text()))]
        inputs.import_document(self.store, dict(schema_version=1, entries=[entry]), artifacts=self.root)
        self.verdict = 'PASS'
        self.assertEqual(self.daily()['passed'], 1)
        ledger = json.loads((self.store / learn_daily.LEDGER).read_text())
        draft = next(iter(ledger['drafts'].values()))
        pairs = learn_daily.fix_pairs(self.store, coldpass_inputs=True)
        sources = learn_replay.SourceProjects(self.root / 'checkouts', self.root, 'example', 30)
        with patch.object(learn_replay, 'source_repo', return_value=self.repo):
            coldpass_replay.preflight(self.store, c.load_store(self.store)[1], pairs, sources)
            pair = next(pair for pair in pairs if pair.get('type') == 'coldpass')
            with self.assertRaisesRegex(c.ColdfixError, 'privacy_blocked'):
                learn_replay.coldpass_proof(self.store, c.load_store(self.store)[1], draft, self.failure, pair, sources, 30)
        self.assertFalse(c.load_store(self.store)[1]['lessons'])

    def test_unicode_wheel_payloads_are_visible_to_privacy_scanning(self):
        import coldpass_replay
        proof = self.entry()['witness']
        credential = 'ghp_' + ''.join((letter.upper() if index % 2 == 0 else letter) + str(index % 10)
                                      for index, letter in enumerate('abcdefghijklmnopqr', 1))
        cases = [('hostname', 'https://private.internal', None),
                 ('credential', credential, None),
                 ('private_term', '秘密の値', {'private_term': ['秘密の値']})]
        for encoding, width in (('utf-16-le', 2), ('utf-16-be', 2), ('utf-32-le', 4), ('utf-32-be', 4)):
            for offset in range(width):
                for category, value, terms in cases:
                    with self.subTest(encoding=encoding, offset=offset, category=category):
                        blob = b'\xff' * offset + value.encode(encoding)
                        counts = coldpass_replay.privacy_counts(proof, {'package.whl': wheel_bytes(blob)}, terms)
                        self.assertGreater(counts.get(category, 0), 0)
        raw = coldpass_replay.privacy_counts(proof, {'package.whl': wheel_bytes(b'\x00\xff\x80binary resource\x00')})
        self.assertGreater(raw.get('control', 0), 0)
        clean = coldpass_replay.privacy_counts(proof, {'setup.py': b'if True:\n    pass\n'})
        self.assertEqual(clean, {})

    def test_wheel_metadata_is_screened_with_payloads(self):
        import coldpass_replay
        proof = self.entry()['witness']
        payload = '秘密の値'.encode('utf-16-le')
        for field in ('archive_comment', 'member_comment', 'extra'):
            with self.subTest(field=field):
                stream = io.BytesIO()
                info = zipfile.ZipInfo('package/data.txt')
                if field == 'member_comment':
                    info.comment = payload
                elif field == 'extra':
                    info.extra = b'XQ' + len(payload).to_bytes(2, 'little') + payload
                with zipfile.ZipFile(stream, 'w') as archive:
                    archive.writestr(info, b'Public data.\n')
                    if field == 'archive_comment':
                        archive.comment = payload
                counts = coldpass_replay.privacy_counts(proof, {'package.whl': stream.getvalue()},
                                                        {'denylist': ['秘密の値']})
                self.assertGreater(counts.get('denylist', 0), 0)

    def test_benign_wheel_metadata_does_not_invent_control_findings(self):
        import coldpass_replay
        stream = io.BytesIO()
        info = zipfile.ZipInfo('package/data.txt')
        info.extra = b'XQ\x02\x00\x00\x01'
        info.comment = 'Public comment.'.encode('utf-16-le')
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr(info, b'Public data.\n')
        self.assertEqual(coldpass_replay.privacy_counts(self.entry()['witness'],
            {'package.whl': stream.getvalue()}), {})

    def test_unicode_wheel_export_honors_private_denylist(self):
        import coldpass_replay
        import regression_adapter
        proof = self.entry()['witness']
        blobs = {'package.whl': wheel_bytes('秘密の値'.encode('utf-16-le'))}
        patch_text = learn_replay.new_file_patch('test_behavior.py', proof['check'])
        script, _, _ = regression_adapter.build_coldpass('test_behavior.py', patch_text, 'test_sibling', proof, blobs)
        denylist = self.root / 'terms.json'
        denylist.write_text(json.dumps(['秘密の値']))
        output = self.root / 'encoded-export.md'
        args = c.parser().parse_args(['--store', str(self.store), 'export', '--output', str(output),
                                     '--denylist', str(denylist)])
        with self.assertRaisesRegex(c.ColdfixError, 'export privacy screen blocked'):
            c.screened_export(args, self.store, c.load_store(self.store)[1], script, [proof['check']],
                [coldpass_replay.privacy_text(proof, blobs)], [(proof, blobs)])
        self.assertFalse(output.exists())

    def test_unparsed_wheel_bytes_cannot_escape_export_denylist(self):
        import coldpass_replay
        import regression_adapter
        proof = self.entry()['witness']
        denylist = self.root / 'terms.json'
        denylist.write_text(json.dumps(['秘密の値']))
        patch_text = learn_replay.new_file_patch('test_behavior.py', proof['check'])
        for location in ('trailer', 'local_extra'):
            for encoding in ('utf-8', 'utf-16-le'):
                with self.subTest(location=location, encoding=encoding):
                    payload = '秘密の値'.encode(encoding)
                    if location == 'trailer':
                        blob = wheel_bytes(b'Public data.\n') + payload
                    else:
                        stream = io.BytesIO()
                        info = zipfile.ZipInfo('package/data.txt')
                        placeholder = b'Q' * len(payload)
                        info.extra = b'XQ' + len(payload).to_bytes(2, 'little') + placeholder
                        with zipfile.ZipFile(stream, 'w') as archive:
                            archive.writestr(info, b'Public data.\n')
                        blob = stream.getvalue().replace(placeholder, payload, 1)
                    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                        self.assertEqual(archive.read('package/data.txt') if location == 'local_extra'
                                         else archive.read('package/data.bin'), b'Public data.\n')
                    blobs = {'package.whl': blob}
                    script, _, _ = regression_adapter.build_coldpass('test_behavior.py', patch_text,
                                                                    'test_sibling', proof, blobs)
                    folder = self.root / (location + encoding)
                    folder.mkdir()
                    output = folder / 'encoded-export.md'
                    args = c.parser().parse_args(['--store', str(self.store), 'export', '--output', str(output),
                                                 '--denylist', str(denylist)])
                    with self.assertRaisesRegex(c.ColdfixError, 'export privacy screen blocked'):
                        c.screened_export(args, self.store, c.load_store(self.store)[1], script, [proof['check']],
                            [coldpass_replay.privacy_text(proof, blobs)], [(proof, blobs)])
                    self.assertFalse(output.exists())

    def test_unicode_wheel_is_blocked_before_admission_executes(self):
        import coldpass_replay
        import replay_isolation
        entry = self.entry()
        wheel = self.root / 'package.whl'
        wheel.write_bytes(wheel_bytes('https://private.internal'.encode('utf-16')))
        entry['witness']['dependencies'] = [dict(path='package.whl', version='1',
                                                 sha256=hashlib.sha256(wheel.read_bytes()).hexdigest())]
        inputs.import_document(self.store, dict(schema_version=1, entries=[entry]), artifacts=self.root)
        self.verdict = 'PASS'
        self.assertEqual(self.daily()['passed'], 1)
        ledger = json.loads((self.store / learn_daily.LEDGER).read_text())
        draft = next(iter(ledger['drafts'].values()))
        pairs = learn_daily.fix_pairs(self.store, coldpass_inputs=True)
        sources = learn_replay.SourceProjects(self.root / 'checkouts', self.root, 'example', 30)
        with patch.object(learn_replay, 'source_repo', return_value=self.repo), \
                patch.object(replay_isolation, 'replay_tree', side_effect=AssertionError('private artifact executed')):
            coldpass_replay.preflight(self.store, c.load_store(self.store)[1], pairs, sources)
            pair = next(pair for pair in pairs if pair.get('type') == 'coldpass')
            with self.assertRaisesRegex(c.ColdfixError, 'privacy_blocked'):
                learn_replay.coldpass_proof(self.store, c.load_store(self.store)[1], draft, self.failure, pair, sources, 30)
        self.assertFalse(c.load_store(self.store)[1]['lessons'])


class ColdpassReviewRegressionTests(unittest.TestCase):
    setUp = fixtures.ColdpassInputTests.setUp
    entry = fixtures.ColdpassInputTests.entry
    write = fixtures.ColdpassInputTests.write
    cli = fixtures.ColdpassInputTests.cli
    stored = fixtures.ColdpassInputTests.stored
    import_pair = ColdpassFlowTests.import_pair
    daily = ColdpassFlowTests.daily
    ask = ColdpassFlowTests.ask

    def screened(self):
        self.import_pair(check='import unittest\nimport behavior\nclass Import(unittest.TestCase):\n'
                         '    def test_sibling(self):\n        self.assertEqual(behavior.value, 1)\n')
        self.verdict = 'PASS'
        self.assertEqual(self.daily()['passed'], 1)
        return json.loads((self.store / learn_daily.LEDGER).read_text())

    def prove(self, ledger):
        sources = learn_replay.SourceProjects(self.root / 'checkouts', self.root, 'example', 60)
        with patch.object(learn_replay, 'source_repo', return_value=self.repo):
            return learn_replay.run_in_workspace(self.store, ledger, sources, learn_replay.PAIRS, 60, history=False)

    def test_unrelated_manifest_import_preserves_existing_screen_and_replay(self):
        ledger = self.screened()
        fingerprint, draft = next(iter(ledger['drafts'].items()))
        screen = copy.deepcopy(draft['coldpass_screen'])
        original = self.failure
        source = fixtures.fixtures
        self.write([source.run_row('unrelated-run', 'blocked', owner='repo', commit=self.before,
                                   what_failed=source.CLASS_TEXT['missing-file'])])
        self.cli()
        self.failure = next(row for row in self.stored() if row['id'] != original['id'])
        other = self.entry()
        other.update(run_id='unrelated-run', failure_class='missing-file')
        inputs.import_document(self.store, dict(schema_version=1, entries=[other]))
        self.failure = original
        pairs = learn_daily.fix_pairs(self.store, coldpass_inputs=True)
        import coldpass_replay
        with patch.object(learn_replay, 'source_repo', return_value=self.repo), patch.dict('os.environ', COLDFIX_OWNER='example'):
            coldpass_replay.prepare_pairs(self.store, c.load_store(self.store)[1], pairs)
        waiting = learn_daily.waiting(c.load_store(self.store)[1], ledger, pairs)
        self.assertNotIn(fingerprint, [row['fingerprint'] for row in waiting])
        self.assertEqual(self.prove(ledger)['proven'], 1)
        self.assertEqual(draft['coldpass_screen'], screen)

    def test_changed_fix_preserves_prior_companion_for_identical_witness(self):
        ledger = self.screened()
        self.assertEqual(self.prove(ledger)['proven'], 1)
        old_files = {path.name: path.read_bytes() for path in (self.store / 'coldpass-admissions').glob('*.json')}
        old_store = c.load_store(self.store)[1]
        old_lesson = next(row for row in c.latest_lessons(old_store).values() if row['verification'] == 'proven')
        entry = inputs.load(self.store)['entries'][0]
        (self.repo / 'README.md').write_text('Documentation after the fix.\n')
        after = fixtures.fixtures.commit(self.repo, 'document fixed behavior')
        entry['upstream']['after'] = after
        inputs.import_document(self.store, dict(schema_version=1, entries=[entry]), revise=True)
        self.assertEqual(self.daily('2026-11-11')['passed'], 1)
        ledger = json.loads((self.store / learn_daily.LEDGER).read_text())
        self.assertEqual(self.prove(ledger)['proven'], 1)
        new_store = c.load_store(self.store)[1]
        current = c.latest_lessons(new_store)[old_lesson['lesson_id']]
        self.assertEqual(current['check_script'], old_lesson['check_script'])
        self.assertEqual(new_store['receipts'][current['receipt']]['after']['commit'], after)
        self.assertEqual({path.name: path.read_bytes() for path in (self.store / 'coldpass-admissions').glob('*.json')
                          if path.name in old_files}, old_files)
        self.assertEqual(len(list((self.store / 'coldpass-admissions').glob('*.json'))), 2)


    def test_coldpass_fix_subject_git_read_has_a_remaining_timeout(self):
        import time
        ledger = self.screened()
        draft = next(iter(ledger['drafts'].values()))
        pair = next(pair for pair in learn_daily.fix_pairs(self.store, coldpass_inputs=True)
                    if pair.get('type') == 'coldpass')
        projects = learn_replay.SourceProjects(self.root / 'checkouts', self.root, 'example', 60)
        read = c.privacy_git
        subjects = []

        def bounded_read(repo, *args, **kwargs):
            if args[0] == 'show':
                subjects.append(kwargs.get('timeout'))
                self.assertIsNotNone(kwargs.get('timeout'))
                self.assertGreater(kwargs['timeout'], 0)
                self.assertLessEqual(kwargs['timeout'], 60)
            return read(repo, *args, **kwargs)

        deadline = time.monotonic() + 60
        with patch.object(learn_replay, 'source_repo', return_value=self.repo), \
                patch.object(c, 'privacy_git', side_effect=bounded_read), \
                patch.object(learn_replay.incident_admission, 'full_suite',
                             wraps=learn_replay.incident_admission.full_suite) as suite:
            learn_replay.coldpass_replay.preflight(self.store, c.load_store(self.store)[1], [pair], projects)
            _, proof = learn_replay.coldpass_proof(self.store, c.load_store(self.store)[1], draft, self.failure,
                                                 pair, projects, 60, deadline)
        self.assertTrue(proof)
        self.assertEqual(suite.call_args.args[-1], deadline)
        self.assertTrue(subjects)

    def test_settled_drafts_do_not_shrink_a_slow_valid_replay_budget(self):
        ledger = self.screened()
        draft = next(iter(ledger['drafts'].values()))
        for index in range(100):
            settled = copy.deepcopy(draft)
            settled.pop('coldpass_screen')
            settled.update(replay=dict(status='rejected'),
                           replay_attempts=[dict(day=learn_replay.utc_day())])
            ledger['drafts']['settled-' + str(index)] = settled
        projects = learn_replay.SourceProjects(self.root / 'checkouts', self.root, 'example', 600)
        clock, limits = [0.0], []
        admit = learn_replay.incident_admission.admit

        def slow_valid(args, root, store):
            limits.append(args.timeout)
            if args.timeout < 5:
                clock[0] += args.timeout
                raise subprocess.TimeoutExpired('valid witness', args.timeout)
            clock[0] += 5
            return admit(args, root, store)

        with patch.object(learn_replay, 'REPLAY_SECONDS', 60), \
                patch('time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(learn_replay, 'source_repo', return_value=self.repo), \
                patch.object(learn_replay.incident_admission, 'admit', side_effect=slow_valid):
            result = learn_replay.run_in_workspace(self.store, ledger, projects, learn_replay.PAIRS, 600)
        self.assertEqual(result['proven'], 1)
        self.assertEqual(limits, [20])
        self.assertTrue(c.load_store(self.store)[1]['receipts'])

    def test_persistent_coldpass_source_does_not_starve_unrelated_drafts(self):
        original = self.screened()
        fingerprint, draft = next(iter(original['drafts'].items()))
        original['drafts']['unrelated'] = dict(draft, coldpass_screen=None)
        original['drafts']['unrelated'].pop('coldpass_screen')
        projects = learn_replay.SourceProjects(self.root / 'checkouts', self.root, 'example', 600)
        clock, acquisitions, unrelated = [0.0], [], []
        select = learn_replay.replay_candidates

        def unavailable(failure, sources, revisions=()):
            acquisitions.append(failure['id'])
            limit = sources.timeout
            clock[0] += min(25, limit)
            raise subprocess.TimeoutExpired('source acquisition', limit)

        def candidates(store, entry, pairs, sources, timeout, reasons, history):
            if entry.get('coldpass_screen'):
                return select(store, entry, pairs, sources, timeout, reasons, history)
            return iter([(self.failure, dict(before=self.before, after=self.after))])

        def prepare(*args, **kwargs):
            unrelated.append('unrelated')
            raise c.ColdfixError('unrelated source unavailable')

        cursor = None
        for _ in range(3):
            ledger = copy.deepcopy(original)
            if cursor is not None:
                ledger['replay_cursor'] = cursor
            with patch.object(learn_replay, 'REPLAY_SECONDS', 10), \
                    patch('time.monotonic', side_effect=lambda: clock[0]), \
                    patch.object(learn_replay, 'source_repo', side_effect=unavailable), \
                    patch.object(learn_replay, 'replay_candidates', side_effect=candidates), \
                    patch.object(learn_replay, 'prepare', side_effect=prepare):
                learn_replay.run_in_workspace(self.store, ledger, projects, learn_replay.PAIRS, 600, history=False)
            cursor = json.loads((self.store / learn_daily.LEDGER).read_text())['replay_cursor']
        self.assertEqual((len(acquisitions), unrelated), (3, ['unrelated'] * 2))
        self.assertEqual(ledger['drafts']['unrelated']['replay']['reason'], 'unrelated source unavailable')

    def test_coldpass_preflight_respects_the_replay_budget(self):
        ledger = self.screened()
        projects = learn_replay.SourceProjects(self.root / 'checkouts', self.root, 'example', 600)
        clock, started = [0.0], []

        def unavailable(failure, sources, revisions=()):
            started.append(failure['id'])
            clock[0] += min(25, sources.timeout)
            raise subprocess.TimeoutExpired('source acquisition', sources.timeout)

        pairs = [pair for pair in learn_daily.fix_pairs(self.store, coldpass_inputs=True)
                 if pair.get('type') == 'coldpass']
        with patch.object(learn_daily, 'fix_pairs', return_value=pairs + copy.deepcopy(pairs)), \
                patch.object(learn_replay, 'REPLAY_SECONDS', 10), \
                patch('time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(learn_replay, 'source_repo', side_effect=unavailable):
            result = learn_replay.run_in_workspace(self.store, ledger, projects, learn_replay.PAIRS, 600)
        self.assertEqual((clock[0], len(started)), (10, 1))
        self.assertEqual((result['proven'], result['rejected']), (0, 1))
        self.assertEqual(next(iter(ledger['drafts'].values()))['replay']['reason'], 'replay_incomplete')
        self.assertEqual(projects.timeout, 600)
        self.assertEqual([pair['blockers'] for pair in pairs], [['replay_incomplete']])
        with patch.object(learn_replay, 'utc_day', return_value='2026-11-12'):
            self.assertEqual(self.prove(ledger)['proven'], 1)

    def test_concurrent_revisions_cannot_borrow_another_active_rule(self):
        self.import_pair()
        original = self.failure
        source = fixtures.fixtures
        self.write([source.run_row('second-class', 'blocked', owner='repo', commit=self.before,
                                   what_failed=source.CLASS_TEXT['missing-file'])])
        self.cli()
        self.failure = next(row for row in self.stored() if row['id'] != original['id'])
        other = self.entry()
        other.update(run_id='second-class', failure_class='missing-file')
        inputs.import_document(self.store, dict(schema_version=1, entries=[other]))
        rules = {}
        revising = False

        def ask(model, prompt, shape, work):
            data = json.loads(prompt.split('\n', 1)[1])
            if shape is learn_daily.DRAFTS:
                for index, row in enumerate(data):
                    rules.setdefault(row['fingerprint'], 'Check behavior number ' + str(index) + '.')
                fingerprints = list(rules)
                return dict(drafts=[dict(fingerprint=row['fingerprint'], kind='test', cause='c', prevention='p', title='Check behavior',
                    rule=rules[fingerprints[1]] if revising else rules[row['fingerprint']], check_command='ignored')
                    for row in data])
            return dict(reviews=[dict(fingerprint=row['fingerprint'], verdict='PASS', reason='Checked.')
                                 for row in data['drafts']])

        with patch.object(self, 'ask', side_effect=ask):
            self.assertEqual(self.daily()['passed'], 2)
            before = json.loads((self.store / learn_daily.LEDGER).read_text())['drafts']
            entries = inputs.load(self.store)['entries']
            for entry in entries:
                entry['witness']['check'] += '\n'
                entry['witness']['check_sha256'] = c.digest(entry['witness']['check'])
            inputs.import_document(self.store, dict(schema_version=1, entries=entries), revise=True)
            revising = True
            summary = self.daily('2026-11-11')
        retained = json.loads((self.store / learn_daily.LEDGER).read_text())
        first, second = list(rules)
        self.assertEqual(summary['passed'], 1)
        self.assertEqual(retained['drafts'][first], before[first])
        self.assertEqual(retained['revisions'][first][-1]['reason'], 'duplicate of draft ' + second)
        self.assertEqual(len({row['draft']['rule'] for row in retained['drafts'].values()}), 2)

    def test_other_failure_in_same_class_cannot_reuse_previous_passing_rule(self):
        ledger = self.screened()
        fingerprint, previous = next(iter(ledger['drafts'].items()))
        original = self.failure
        old_input = inputs.load(self.store)['entries'][0]
        source = fixtures.fixtures
        self.write([source.run_row('another-member', 'blocked', owner='repo', commit=self.before,
                                   what_failed=source.SIBLING)])
        self.cli()
        self.failure = next(row for row in self.stored() if row['id'] != original['id'])
        other = self.entry()
        other.update(run_id='another-member', witness=old_input['witness'])
        inputs.import_document(self.store, dict(schema_version=1, entries=[other]))
        store = c.load_store(self.store)[1]
        candidate = next(row for row in learn_candidates.candidates(store) if self.failure['id'] in row['evidence'])
        self.assertEqual(candidate['fingerprint'], fingerprint)
        summary = self.daily('2026-11-11')
        self.assertEqual((summary['passed'], summary['calls']), (0, 1))
        retained = json.loads((self.store / learn_daily.LEDGER).read_text())
        self.assertEqual(retained['drafts'][fingerprint], previous)
        self.assertEqual(retained['revisions'][fingerprint][-1]['verdict'], 'FAIL')
        self.assertEqual(retained['revisions'][fingerprint][-1]['reason'], 'duplicate of draft ' + fingerprint)


class ColdpassReplayPinTests(unittest.TestCase):
    setUp = fixtures.ColdpassInputTests.setUp
    entry = fixtures.ColdpassInputTests.entry
    write = fixtures.ColdpassInputTests.write
    cli = fixtures.ColdpassInputTests.cli
    stored = fixtures.ColdpassInputTests.stored
    import_pair = ColdpassFlowTests.import_pair
    daily = ColdpassFlowTests.daily
    ask = ColdpassFlowTests.ask
    screened = ColdpassReviewRegressionTests.screened
    prove = ColdpassReviewRegressionTests.prove

    def pinned_lesson(self):
        self.assertEqual(self.prove(self.screened())['proven'], 1)
        store = c.load_store(self.store)[1]
        lesson = next(row for row in c.latest_lessons(store).values() if row['verification'] == 'proven')
        other_before = subprocess.check_output(['git', '-C', str(self.repo), '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.invalid', 'commit-tree', self.before + '^{tree}',
            '-p', self.before, '-m', 'unreviewed failing revision'], text=True).strip()
        (self.repo / 'README.md').write_text('Unreviewed fixed descendant.\n')
        other_after = fixtures.fixtures.commit(self.repo, 'unreviewed fixed descendant')
        return lesson, store['receipts'][lesson['receipt']], other_before, other_after

    def test_replay_cli_rejects_revisions_outside_screened_pair(self):
        lesson, _, other_before, other_after = self.pinned_lesson()
        immutable = {str(path.relative_to(self.store)): path.read_bytes()
                     for folder in ('lessons', 'receipts') for path in (self.store / folder).glob('*.json')}
        for before, after in ((other_before, self.after), (self.before, other_after)):
            with self.subTest(before=before, after=after):
                result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'coldfix.py'),
                    '--store', str(self.store), 'replay', '--lesson', lesson['lesson_id'], '--repo', str(self.repo),
                    '--before', before, '--after', after, '--status', 'active', '--timeout', '60'],
                    text=True, capture_output=True)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('revision_conflict', result.stderr)
        self.assertEqual({str(path.relative_to(self.store)): path.read_bytes()
                          for folder in ('lessons', 'receipts') for path in (self.store / folder).glob('*.json')}, immutable)
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'coldfix.py'),
            '--store', str(self.store), 'replay', '--lesson', lesson['lesson_id'], '--repo', str(self.repo),
            '--before', self.before, '--after', self.after, '--status', 'active', '--timeout', '60'],
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['verification'], 'proven')

    def test_store_rejects_receipt_commits_outside_screened_pair(self):
        lesson, receipt, other_before, other_after = self.pinned_lesson()
        path = self.store / 'lessons' / (lesson['id'] + '.json')
        original = path.read_bytes()
        for side, commit in (('before', other_before), ('after', other_after), ('reversed', other_after)):
            with self.subTest(side=side):
                altered = copy.deepcopy(receipt)
                altered[side]['commit'] = commit
                if side == 'after':
                    altered['reversed']['commit'] = commit
                altered['id'] = c.receipt_id(altered)
                c.save(self.store, 'receipts', altered)
                path.write_text(c.encoded(dict(lesson, receipt=altered['id'])))
                try:
                    with self.assertRaisesRegex(c.ColdfixError, 'revision_conflict'):
                        c.load_store(self.store)
                finally:
                    path.write_bytes(original)
        self.assertEqual(c.load_store(self.store)[1]['receipts'][receipt['id']], receipt)


if __name__ == '__main__':
    unittest.main()
