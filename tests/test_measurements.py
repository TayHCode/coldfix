"""Synthetic evidence only. Exercise measurements through the real CLI."""

import concurrent.futures
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import coldfix as c


CLI = Path(c.__file__).resolve()


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = Path(self.temporary.name)
        self.store = self.cwd / '.coldfix'
        self.ids = [self.event('2025-01-09T12:00:00Z'), self.event('2025-01-09T18:00:00Z')]
        self.lesson = self.proven_lesson()

    def event(self, at, **changes):
        row = dict(command='check', error='Expected two', repo='example', agent='worker', kind='test',
                   source_ref='sample:' + c.new_record()['id'], occurred=at)
        row.update(changes)
        record = c.event_record(row, 'synthetic-source')
        c.save(self.store, 'failures', record)
        return record['id']

    def proven_lesson(self, status='enforced', grouping='exact', previous=None, created='2025-01-01T00:00:00Z'):
        failures = {p.stem: json.loads(p.read_text()) for p in (self.store / 'failures').glob('*.json')}
        row = dict(c.new_record(), version=2, created=created, lesson_id='', previous='',
                   group_id=failures[self.ids[0]]['group_id'] if grouping == 'exact' else c.signature(failures[self.ids[0]]),
                   grouping=grouping, evidence=sorted(self.ids), kind='test', title='Count', rule='Check the count',
                   check_command='python3 {check}', check_script='example synthetic script', expected_failure='count',
                   level='test', scope='repo:example', status=status, verification='proven', receipt=None)
        row['lesson_id'] = row['id']
        if previous:
            row.update(id=c.revision_id(previous['id']), previous=previous['id'], lesson_id=previous['lesson_id'])
        receipt = dict(c.new_record(), lesson_revision=row['id'], lesson_digest=c.lesson_digest(row),
                       check_sha256=c.digest(row['check_script']), check_command=row['check_command'], repo='example',
                       before=dict(commit='a'*40, tree='c'*40, status='FAIL', exit=1,
                                   stdout='{"status":"FAIL","failure":"count"}', stderr=''),
                       after=dict(commit='b'*40, tree='d'*40, status='PASS', exit=0, stdout='{"status":"PASS"}', stderr=''),
                       status='PASS')
        receipt['id'] = c.receipt_id(receipt)
        row['receipt'] = receipt['id']
        c.save(self.store, 'receipts', receipt)
        c.save(self.store, 'lessons', row)
        return row

    def evidence(self, activation=None):
        return dict(version=1, activations=[dict(lesson_revision=self.lesson['id'],
                    at=activation or '2025-01-10T12:00:00Z', commit='e'*40, evidence_ref='delivery:synthetic')],
                    expected_sources=['example'], coverage=[dict(source='example', repos=['example'],
                    since='2025-01-01', until='2025-02-01', items_read=12, complete=True, gaps=[], evidence_ref='collection:synthetic')])

    def run_cli(self, *args, code=2):
        result = subprocess.run([sys.executable, str(CLI), '--store', str(self.store), *args],
                                cwd=self.cwd, text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        return result

    def measure(self, data=None, code=2, days=1):
        args = ['measure', '--window-days', str(days)]
        if data is not None:
            path = self.cwd / 'input.json'
            path.write_text(json.dumps(data))
            args += ['--input', str(path)]
        result = self.run_cli(*args, code=code)
        self.assertNotIn('delivery:synthetic', result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        report = json.loads((self.store / 'measurements' / (summary['report_id'] + '.json')).read_text())
        outcomes = [json.loads((self.store / 'outcomes' / (r + '.json')).read_text()) for r in summary['outcome_ids']]
        self.run_cli('lesson', code=0)
        return report, outcomes

    def test_equal_windows_boundary_instants_and_duplicate_imports(self):
        before = self.event('2025-01-09T12:00:00Z', source_ref='sample:boundary')
        duplicate = self.event('2025-01-09T12:00:00Z', source_ref='sample:boundary')
        zoned_duplicate = self.event('2025-01-09T13:00:00+01:00', source_ref='sample:boundary')
        at = self.event('2025-01-10T12:00:00+00:00')
        last = self.event('2025-01-11T11:59:59Z')
        excluded = self.event('2025-01-11T12:00:00Z')
        report, (outcome,) = self.measure(self.evidence())
        self.assertEqual(outcome['recurrences'], dict(before=3, after=2))
        self.assertEqual(outcome['status'], 'PASS')
        self.assertEqual(outcome['evidence']['before'], sorted([*self.ids, before, duplicate, zoned_duplicate]))
        self.assertEqual(outcome['evidence']['after'], sorted([at, last]))
        self.assertNotIn(excluded, outcome['evidence']['after'])
        self.assertEqual(outcome['evidence']['duplicates'], [sorted([before, duplicate, zoned_duplicate])])
        self.assertEqual(outcome['activation']['commit'], 'e'*40)
        self.assertEqual(outcome['replay_result']['after']['commit'], 'b'*40)
        self.assertEqual(outcome['window']['before'], ['2025-01-09T12:00:00+00:00','2025-01-10T12:00:00+00:00'])
        self.assertEqual(outcome['window']['after'], ['2025-01-10T12:00:00+00:00','2025-01-11T12:00:00+00:00'])
        self.assertEqual(report['model_calls']['measurement_calls'], 0)
        self.assertEqual(report['status'], 'INCOMPLETE')

    def test_date_precision_activation_day_excluded_and_straddling_event_unknown(self):
        after = self.event('2025-01-11')
        activation_day = self.event('2025-01-10')
        _, (outcome,) = self.measure(self.evidence('2025-01-10'))
        self.assertEqual(outcome['recurrences'], dict(before=2, after=1))
        self.assertEqual(outcome['evidence']['after'], [after])
        self.assertNotIn(activation_day, outcome['evidence']['before'] + outcome['evidence']['after'])
        _, (outcome,) = self.measure(self.evidence())
        self.assertIsNone(outcome['recurrences']['after'])
        self.assertEqual(outcome['evidence']['uncertain'], sorted([after, activation_day]))

    def test_absent_activation_coverage_dates_and_labels_remain_unknown(self):
        report, (outcome,) = self.measure()
        self.assertEqual(outcome['recurrences'], dict(before=None, after=None))
        self.assertIsNone(outcome['activation'])
        self.assertEqual(outcome['replay_result']['status'], 'PASS')
        self.assertEqual(report['dedup']['by_grouping']['exact']['false_merges'], None)
        self.assertEqual(report['privacy']['status'], 'INCOMPLETE')
        self.assertEqual(report['model_calls']['runs'], [])
        old = dict(c.new_record(), group_id='', command='check', error='Expected two', repo='example',
                   agent='worker', thread='', source='stdin')
        old['group_id'] = c.group_id(old)
        c.save(self.store, 'failures', old)
        data = self.evidence()
        data['coverage'][0]['gaps'] = ['permission denied']
        _, (outcome,) = self.measure(data)
        self.assertEqual(outcome['evidence']['undated'], [old['id']])
        self.assertIsNone(outcome['recurrences']['before'])
        self.assertTrue(any('coverage' in gap for gap in outcome['gaps']))

    def test_incomplete_coverage_cases_and_unelapsed_window(self):
        cases = [dict(items_read=None), dict(complete=False), dict(gaps=['pagination interrupted']),
                 dict(repos=['other']), dict(since='2025-01-10'), dict(until='2025-01-11')]
        for changes in cases:
            with self.subTest(changes=changes):
                data = self.evidence()
                data['coverage'][0].update(changes)
                _, (outcome,) = self.measure(data)
                self.assertEqual(outcome['status'], 'INCOMPLETE')
                self.assertIsNone(outcome['recurrences']['after'])
        data = self.evidence('2099-01-10')
        data['coverage'][0].update(since='2099-01-01', until='2099-02-01')
        _, (outcome,) = self.measure(data)
        self.assertIn('After window has not elapsed.', outcome['gaps'])

    def test_conflicting_duplicate_dates_do_not_inflate_recurrence(self):
        one = self.event('2025-01-10T13:00:00Z', source_ref='same:item')
        two = self.event('2025-01-09T13:00:00Z', source_ref='same:item')
        _, (outcome,) = self.measure(self.evidence())
        self.assertEqual(outcome['observed'], dict(before=2, after=0))
        self.assertEqual(outcome['evidence']['uncertain'], sorted([one, two]))
        self.assertIsNone(outcome['recurrences']['after'])

    def test_lifecycle_active_enforced_retired_and_historical_completed_window(self):
        active = self.proven_lesson(status='active', previous=self.lesson, created='2025-01-10T18:00:00Z')
        retired = dict(c.next_revision(active), created='2025-01-15T00:00:00Z', status='retired',
                       verification='unproven', receipt=None)
        c.save(self.store, 'lessons', retired)
        data = self.evidence()
        data['activations'].append(dict(lesson_revision=active['id'], at='2025-01-11T00:00:00Z',
                                        commit='f'*40, evidence_ref='delivery:later'))
        _, outcomes = self.measure(data)
        by_id = {r['lesson_revision']: r for r in outcomes}
        self.assertEqual(by_id[self.lesson['id']]['status'], 'INCOMPLETE')
        self.assertEqual(by_id[active['id']]['status'], 'PASS')
        self.assertEqual(by_id[active['id']]['latest_status'], 'retired')
        _, (unknown,) = self.measure()
        self.assertEqual(unknown['latest_status'], 'retired')
        self.assertEqual(unknown['replay_result']['status'], 'INCOMPLETE')
        c.save(self.store, 'lessons', dict(c.next_revision(retired), created='2025-01-16T00:00:00Z', status='superseded'))
        _, (superseded,) = self.measure()
        self.assertEqual(superseded['latest_status'], 'superseded')

    def test_normalized_groups_and_exact_groups_use_the_lesson_binding(self):
        normalized = self.proven_lesson(grouping='normalized')
        self.event('2025-01-10T13:00:00Z', repo='another')
        data = self.evidence()
        data['activations'].append(dict(lesson_revision=normalized['id'], at='2025-01-10T12:00:00Z',
                                        commit='f'*40, evidence_ref='delivery:normalized'))
        data['coverage'][0]['repos'].append('another')
        _, outcomes = self.measure(data)
        by_group = {r['grouping']: r for r in outcomes}
        self.assertEqual(by_group['exact']['recurrences'], dict(before=2, after=0))
        self.assertEqual(by_group['normalized']['recurrences'], dict(before=2, after=1))

    def test_labeled_extraction_dedup_privacy_and_calls_complete_report(self):
        different = self.event('2025-01-10T13:00:00Z', repo='another')
        failures = c.load_store(self.store)[1]['failures']
        data = self.evidence()
        data['extraction'] = [dict(source='example', source_ref=failures[self.ids[0]]['source_ref'],
                                   expected=True),
                              dict(source='example', source_ref=failures[different]['source_ref'],
                                   expected=False),
                              dict(source='example', source_ref='sample:missed', expected=True),
                              dict(source='example', source_ref='sample:negative', expected=False)]
        data['pairs'] = [dict(left=self.ids[0], right=self.ids[1], same=False),
                         dict(left=self.ids[0], right=different, same=True)]
        data['privacy'] = [dict(case_id='seeded-email', blocked=True, leaks=0, evidence_ref='test:receipt')]
        data['model_runs'] = [dict(run_id='source-run', complete=True, evidence_ref='log:run',
                                  calls=[dict(role='learning-collector', resolved_target='example route', evidence_ref='log:call')])]
        report, _ = self.measure(data, code=0)
        score, = report['extraction']['by_source']
        self.assertEqual({k: score[k] for k in ('tp','fp','fn','tn','precision','recall')},
                         dict(tp=1, fp=1, fn=1, tn=1, precision=0.5, recall=0.5))
        exact = report['dedup']['by_grouping']['exact']
        normal = report['dedup']['by_grouping']['normalized']
        self.assertEqual((exact['false_merges'], exact['missed_merges']), (1, 1))
        self.assertEqual((normal['false_merges'], normal['missed_merges']), (1, 0))
        self.assertEqual(report['sources']['inventory'][0]['records'], 3)
        self.assertEqual(report['sources']['coverage'][0]['items_read'], 12)
        self.assertEqual(report['model_calls']['runs'][0]['calls_by_role'], {'learning-collector': 1})
        self.assertEqual(report['status'], 'PASS')
        data['model_runs'][0]['complete'] = False
        data['privacy'][0]['leaks'] = 1
        report, _ = self.measure(data, code=1)
        self.assertIsNone(report['model_calls']['runs'][0]['calls_by_role'])
        self.assertEqual(report['privacy']['status'], 'FAIL')

    def test_zero_denominators_are_unknown_and_other_sources_are_listed(self):
        data = self.evidence()
        data['expected_sources'].append('unsampled')
        data['extraction'] = [dict(source='example', source_ref='sample:negative', expected=False)]
        report, _ = self.measure(data)
        example, missing = report['extraction']['by_source']
        self.assertEqual((example['precision'], example['recall']), (None, None))
        self.assertEqual((missing['source'], missing['samples'], missing['status']), ('unsampled', 0, 'INCOMPLETE'))

    def test_malformed_evidence_writes_nothing_and_diagnostics_are_value_free(self):
        cases = [dict(version=True), dict(unknown='sensitive-value'), dict(activations=[{}]),
                 dict(coverage=[{}]), dict(pairs=[dict(left=self.ids[0], right=self.ids[0], same=True)]),
                 dict(extraction=[dict(source='s', source_ref='wrong', expected='yes')]),
                 dict(privacy=[dict(case_id='sensitive-value', blocked='yes', leaks=0, evidence_ref='test')]),
                 dict(model_runs=[dict(run_id='x', complete=True, calls=[{}], evidence_ref='log')])]
        for changes in cases:
            data = self.evidence()
            data.update(changes)
            path = self.cwd / 'bad.json'
            path.write_text(json.dumps(data))
            result = self.run_cli('measure', '--input', str(path))
            self.assertIn('invalid measurement evidence', result.stderr)
            self.assertNotIn('sensitive-value', result.stdout + result.stderr)
            self.assertFalse((self.store / 'measurements').exists())
            self.assertFalse((self.store / 'outcomes').exists())
        path.write_text('{"version":1,"version":1}')
        self.assertIn('invalid measurement evidence', self.run_cli('measure','--input',str(path)).stderr)
        self.run_cli('measure','--window-days','0')
        self.run_cli('measure','--window-days','999999999999')

    def test_immutable_private_outputs_and_original_records_survive_reruns(self):
        originals = {p: p.read_bytes() for p in self.store.rglob('*.json')}
        first, _ = self.measure(self.evidence())
        old_outputs = {p: p.read_bytes() for p in self.store.rglob('*.json')}
        self.event('2025-01-10T14:00:00Z')
        second, _ = self.measure(self.evidence())
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual({p: p.read_bytes() for p in old_outputs}, old_outputs)
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        for folder in ('measurements','outcomes'):
            self.assertEqual((self.store / folder).stat().st_mode & 0o777, 0o700)
            for path in (self.store / folder).glob('*.json'):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_report_outcome_and_snapshot_refuse_further_writes(self):
        report, (outcome,) = self.measure(self.evidence())
        for collection, row in (('measurements', report), ('outcomes', outcome)):
            path = self.store / collection / (row['id']+'.json')
            original = path.read_bytes()
            path.write_text(json.dumps(dict(row,status='PASS' if row['status'] != 'PASS' else 'FAIL')))
            result = self.run_cli('measure')
            self.assertIn('corrupt store record', result.stderr)
            path.write_bytes(original)
        path = self.store / 'failures' / (self.ids[0]+'.json')
        original = path.read_bytes()
        row = json.loads(original)
        row['agent'] = 'changed'
        path.write_text(json.dumps(row))
        self.assertIn('measurement',self.run_cli('measure').stderr)
        path.write_bytes(original)
        self.run_cli('lesson', code=0)

    def test_permissions_symlinks_and_interrupted_report_publication(self):
        self.store.chmod(0o755)
        self.assertIn('private', self.run_cli('measure').stderr)
        self.store.chmod(0o700)
        collection = self.store / 'outcomes'
        collection.mkdir(mode=0o755)
        collection.chmod(0o755)
        self.assertIn('private', self.run_cli('measure').stderr)
        collection.chmod(0o700)
        target = self.cwd / 'target'
        target.mkdir()
        (self.store / 'measurements').symlink_to(target, target_is_directory=True)
        self.assertIn('symlink', self.run_cli('measure').stderr)
        (self.store / 'measurements').unlink()
        from measurements import measure
        from argparse import Namespace
        root, store = c.load_store(self.store)
        real_save = c.save
        def interrupted(root, collection, record):
            if collection == 'measurements':
                raise OSError('synthetic interruption')
            return real_save(root, collection, record)
        with patch('measurements.save', side_effect=interrupted):
            with self.assertRaises(OSError):
                measure(Namespace(input=None,window_days=1), root, store)
        self.assertEqual(len(list((self.store/'outcomes').glob('*.json'))),1)
        self.measure()
        self.assertEqual(len(list((self.store/'outcomes').glob('*.json'))),2)

    def test_empty_store_report_has_unknown_metrics_and_no_outcomes(self):
        self.store = self.cwd / 'empty'
        report, outcomes = self.measure()
        self.assertEqual(outcomes, [])
        self.assertEqual(report['sources']['inventory'], [])
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertIsNone(report['privacy']['leaks'])
        self.assertIsNone(report['privacy']['blocked'])
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)

    def test_resealed_report_cannot_change_computed_results(self):
        report, _ = self.measure(self.evidence())
        original = self.store / 'measurements' / (report['id']+'.json')
        report['sources']['inventory'][0]['records'] = 999
        report['id'] = c.receipt_id(report)
        c.save(self.store, 'measurements', report)
        self.assertIn('corrupt measurement evidence', self.run_cli('measure').stderr)
        self.assertTrue(original.is_file())

    def test_private_input_dates_delivery_and_duplicate_evidence_are_validated(self):
        data = self.evidence()
        cases = []
        for field, value in (('at','2025-02-30'),('at','2025-01-10T00:00:00'),('commit','short'),
                             ('lesson_revision','0'*32),('evidence_ref','')):
            changed = json.loads(json.dumps(data))
            changed['activations'][0][field] = value
            cases.append(changed)
        for field, value in (('items_read',True),('complete',1),('gaps',['']),('until','2024-01-01')):
            changed = json.loads(json.dumps(data))
            changed['coverage'][0][field] = value
            cases.append(changed)
        for key in ('activations','coverage'):
            changed = json.loads(json.dumps(data))
            changed[key] += changed[key]
            cases.append(changed)
        for index, changed in enumerate(cases):
            with self.subTest(index=index):
                path = self.cwd / 'bad.json'
                path.write_text(json.dumps(changed))
                self.assertIn('invalid measurement evidence', self.run_cli('measure','--input',str(path)).stderr)
                self.assertFalse((self.store/'measurements').exists())

    def test_concurrent_reports_use_separate_run_ids(self):
        def run(_):
            return self.run_cli('measure').stdout
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = [json.loads(r) for r in pool.map(run, range(2))]
        self.assertEqual(len({r['report_id'] for r in results}),2)
        self.run_cli('lesson',code=0)

    def test_activation_cannot_predate_revision_but_same_day_precision_is_valid(self):
        for at in ('2024-12-31T23:59:59Z', '2024-12-31'):
            with self.subTest(at=at):
                data = self.evidence(at)
                data['coverage'][0]['since'] = '2024-12-01'
                _, (outcome,) = self.measure(data)
                self.assertEqual(outcome['status'], 'INCOMPLETE')
                self.assertEqual(outcome['recurrences'], dict(before=None, after=None))
                self.assertTrue(any('predates' in gap for gap in outcome['gaps']))
        for at in ('2025-01-01T00:00:00Z', '2025-01-01'):
            with self.subTest(at=at):
                data = self.evidence(at)
                data['coverage'][0]['since'] = '2024-12-01'
                _, (outcome,) = self.measure(data)
                self.assertEqual(outcome['status'], 'PASS')
                self.assertEqual(outcome['recurrences'], dict(before=0, after=0))
        day_lesson = self.proven_lesson(created='2025-01-01T18:00:00Z')
        data = self.evidence()
        data['coverage'][0]['since'] = '2024-12-01'
        data['activations'].append(dict(lesson_revision=day_lesson['id'],at='2025-01-01',
                                        commit='f'*40,evidence_ref='delivery:creation-day'))
        _, outcomes = self.measure(data)
        day_outcome = next(r for r in outcomes if r['lesson_revision'] == day_lesson['id'])
        self.assertEqual(day_outcome['status'], 'PASS')
        self.assertEqual(day_outcome['recurrences'], dict(before=0, after=0))

    def capture_during_measurement_publication(self, collection):
        original_glob = Path.glob
        published = []
        before = len(list((self.store/'failures').glob('*.json')))
        def concurrent_publish(path, pattern):
            paths = list(original_glob(path, pattern))
            if path == self.store/collection and not published:
                published.append(True)
                if collection == 'failures':
                    self.event('2025-01-10T13:00:00Z')
                self.run_cli('measure')
            return iter(paths)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(Path, 'glob', concurrent_publish), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = c.main(['--store',str(self.store),'capture','--command','check',
                                '--repo','example','--agent','worker','--error','Expected two'])
        self.assertEqual(published, [True])
        self.assertEqual(exit_code, 0, stderr.getvalue())
        summary = json.loads(stdout.getvalue())
        self.assertTrue((self.store/'failures'/(summary['failure_id']+'.json')).is_file())
        loaded = c.load_store(self.store)[1]
        self.assertEqual(len(loaded['failures']), before+1+(collection=='failures'))
        self.assertEqual(len(loaded['measurements']), 1)

    def test_capture_accepts_report_published_after_outcome_enumeration(self):
        self.capture_during_measurement_publication('outcomes')

    def test_capture_accepts_report_bound_to_concurrently_ingested_source(self):
        self.capture_during_measurement_publication('failures')


if __name__ == '__main__':
    unittest.main()
