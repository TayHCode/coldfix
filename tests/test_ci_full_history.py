import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ci_fix_learning as ci
from tests.test_ci_fix_learning import FakeGitHub, workflow_run, job


class FullHistoryTests(unittest.TestCase):
    def test_unfiltered_workflow_history_reads_beyond_one_thousand(self):
        gh = ci.GitHub('example/project', full_history=True)
        def page(endpoint):
            self.assertNotIn('status=', endpoint)
            number = int(endpoint.split('page=')[-1])
            start = (number - 1) * 100
            return dict(total_count=1001, workflow_runs=[dict(id=n) for n in range(start, min(start + 100, 1001))])
        with patch.object(gh, 'api', side_effect=page) as api:
            rows = gh.pages('actions/runs', 'workflow_runs', bounded=True)
        self.assertEqual(len(rows), 1001)
        self.assertEqual(api.call_count, 11)
        self.assertEqual(gh.incomplete, [])

    def test_full_history_has_no_failure_or_external_commit_selection_cap(self):
        runs = [workflow_run(n, format(n, '040x')) for n in range(1, 26)]
        gh = FakeGitHub(runs, {n: [] for n in range(1, 26)})
        gh.full_history = True
        with patch.object(gh, 'pages', wraps=gh.pages) as pages:
            rows = ci.collect(gh, None)
        self.assertEqual(len(rows), 25)
        self.assertEqual(gh.incomplete, [])
        commit_call = next(call for call in pages.call_args_list if call.args[0] == 'commits')
        self.assertIsNone(commit_call.kwargs.get('limit'))

    def test_full_history_does_not_request_the_filtered_workflow_endpoint(self):
        gh = FakeGitHub([], {})
        gh.full_history = True
        with patch.object(gh, 'pages', wraps=gh.pages) as pages:
            self.assertEqual(ci.collect(gh, None), [])
        self.assertEqual(pages.call_args_list[0].kwargs.get('query', ''), '')

    def test_persistent_cache_resumes_without_spending_a_second_GET(self):
        with tempfile.TemporaryDirectory() as name:
            cache = Path(name) / 'cache'
            cache.mkdir(mode=0o700)
            response = b'HTTP/2.0 200 OK\r\nx-ratelimit-remaining: 1001\r\n\r\n{"total_count":0,"jobs":[]}'
            with patch.object(ci, 'run', return_value=response) as get:
                session = ci.HistorySession(cache, remaining=1002)
                first = session.read('example/project', 'actions/runs/1/attempts/1/jobs?per_page=100&page=1')
                second = ci.HistorySession(cache, remaining=1000).read('example/project', 'actions/runs/1/attempts/1/jobs?per_page=100&page=1')
            self.assertEqual(first, second)
            self.assertEqual(get.call_count, 1)
            self.assertIn('--include', get.call_args.args[0])
            self.assertEqual(session.remaining, 1001)
            self.assertEqual(next(cache.glob('*.json')).stat().st_mode & 0o777, 0o600)

    def test_reserve_pauses_before_a_network_request(self):
        with tempfile.TemporaryDirectory() as name:
            session = ci.HistorySession(Path(name), remaining=1000)
            with patch.object(ci, 'run') as network:
                with self.assertRaises(ci.BudgetPause):
                    session.read('example/project', 'actions/runs')
            network.assert_not_called()

    def test_pause_is_not_mislabeled_as_source_failure(self):
        with tempfile.TemporaryDirectory() as name:
            gh = FakeGitHub([], {})
            with patch.object(ci, 'GitHub', return_value=gh), patch.object(ci, 'collect', side_effect=ci.BudgetPause):
                result = ci.execute(argparse.Namespace(store=str(Path(name) / '.coldfix'), repo=['example/project'],
                    owner=None, max_failures=20, timeout=1))
            report = json.loads(Path(result['report']).read_text())
            self.assertTrue(report['paused'])
            self.assertFalse(report['enumeration_complete'])
            self.assertEqual(report['repositories'][0]['status'], 'BUDGET_PAUSED')

    def test_full_history_resumes_completed_repositories_without_replaying_them(self):
        with tempfile.TemporaryDirectory() as name:
            args = argparse.Namespace(store=str(Path(name) / '.coldfix'), repo=['example/project'],
                owner=None, max_failures=20, timeout=1, full_history=True, history_cache=None)
            gh = FakeGitHub([], {})
            with patch.object(ci, 'run', return_value=b'{"resources":{"core":{"remaining":2000}}}'), patch.object(ci, 'GitHub', return_value=gh), patch.object(ci, 'collect', return_value=[]) as collect:
                first = ci.execute(args)
                second = ci.execute(args)
            collect.assert_called_once_with(gh, None)
            for summary in (first, second):
                report = json.loads(Path(summary['report']).read_text())
                self.assertTrue(report['enumeration_complete'])
                self.assertIsNone(report['limits']['workflow_history_items_per_repo'])
                self.assertIsNone(report['limits']['external_commits_per_repo'])
            self.assertEqual(json.loads(Path(second['report']).read_text())['api_requests'], 2)

    def test_in_progress_runs_are_not_frozen_into_the_persistent_cache(self):
        with tempfile.TemporaryDirectory() as name:
            cache = Path(name) / 'cache'
            cache.mkdir(mode=0o700)
            body = json.dumps(dict(total_count=1, workflow_runs=[dict(id=1, status='in_progress', conclusion=None)]))
            response = b'HTTP/2.0 200 OK\r\nx-ratelimit-remaining: 2000\r\n\r\n' + body.encode()
            with patch.object(ci, 'run', return_value=response) as get:
                for _ in range(2):
                    ci.HistorySession(cache, remaining=2000).read('example/project', 'actions/runs?per_page=100&page=1')
            self.assertEqual(get.call_count, 2)

    def reads(self, endpoint, value):
        with tempfile.TemporaryDirectory() as name:
            cache = Path(name) / 'cache'
            cache.mkdir(mode=0o700)
            response = b'HTTP/2.0 200 OK\r\nx-ratelimit-remaining: 2000\r\n\r\n' + json.dumps(value).encode()
            with patch.object(ci, 'run', return_value=response) as get:
                for _ in range(2):
                    ci.HistorySession(cache, remaining=2000).read('example/project', endpoint)
            return get.call_count

    def test_shifting_history_pages_are_never_frozen(self):
        completed = [dict(id=1, status='completed', conclusion='failure')]
        self.assertEqual(self.reads('actions/runs?per_page=100&page=2', dict(total_count=101, workflow_runs=completed)), 2)
        self.assertEqual(self.reads('commits?per_page=100&page=2', [dict(sha='a' * 40)]), 2)

    def test_snapshot_bounded_settled_pages_are_reused_on_resume(self):
        bound = '2026-11-05T00:00:00Z'
        completed = [dict(id=1, status='completed', conclusion='failure')]
        self.assertEqual(self.reads('actions/runs?per_page=100&page=2&created=2018-01-01T00:00:00Z..' + bound, dict(total_count=101, workflow_runs=completed)), 1)
        self.assertEqual(self.reads('commits?per_page=100&page=2&until=' + bound, [dict(sha='a' * 40)]), 1)
        self.assertEqual(self.reads('commits/' + 'a' * 40 + '/check-suites?per_page=100&page=1', dict(total_count=0, check_suites=[])), 1)
        self.assertEqual(self.reads('check-suites/5/check-runs?per_page=100&page=1&filter=all', dict(total_count=0, check_runs=[])), 1)

    def test_full_history_listings_are_bounded_by_the_snapshot(self):
        gh = ci.GitHub('example/project', full_history=True, snapshot='2026-11-05T00:00:00Z')
        endpoints = []
        def page(endpoint):
            endpoints.append(endpoint)
            if endpoint.startswith('actions/runs?'):
                return dict(total_count=0, workflow_runs=[])
            if endpoint.startswith('commits?'):
                return [dict(sha='a' * 40)]
            if '/check-suites?' in endpoint:
                return dict(total_count=2, check_suites=[dict(id=5, created_at='2026-11-04T00:00:00Z'),
                                                         dict(id=6, created_at='2026-11-06T00:00:00Z')])
            return dict(total_count=2, check_runs=[
                dict(id=50, conclusion='failure', started_at='2026-11-04T00:00:00Z', app=dict(slug='ci')),
                dict(id=51, conclusion='failure', started_at='2026-11-06T00:00:00Z', app=dict(slug='ci'))])
        with patch.object(gh, 'api', side_effect=page):
            failures = ci.collect(gh, None)
        self.assertTrue(endpoints[0].endswith('..2026-11-05T00:00:00Z'))
        self.assertTrue(any(e.startswith('commits?') and e.endswith('&until=2026-11-05T00:00:00Z') for e in endpoints))
        self.assertFalse(any(e.startswith('check-suites/6/') for e in endpoints))
        self.assertEqual([f.run['id'] for f in failures], [50])

    def test_full_history_splits_run_search_windows_past_one_thousand(self):
        from datetime import datetime, timedelta, timezone
        origin = datetime(2025, 1, 1, tzinfo=timezone.utc)
        stamps = [origin + timedelta(hours=n) for n in range(2500)]
        gh = ci.GitHub('example/project', full_history=True, snapshot='2026-11-05T00:00:00Z')
        def page(endpoint):
            created = endpoint.split('&created=')[1]
            start, end = (datetime.fromisoformat(part.replace('Z', '+00:00')) for part in created.split('..'))
            number = int(endpoint.split('&page=')[1].split('&')[0])
            rows = [dict(id=n, created_at=t.strftime('%Y-%m-%dT%H:%M:%SZ')) for n, t in enumerate(stamps) if start <= t <= end]
            if len(rows) > 1000:
                rows = rows[:1000]
                total = len([t for t in stamps if start <= t <= end])
            else:
                total = len(rows)
            return dict(total_count=total, workflow_runs=rows[(number - 1) * 100:number * 100])
        with patch.object(gh, 'api', side_effect=page):
            rows = ci.history_runs(gh)
        self.assertEqual(sorted(row['id'] for row in rows), list(range(2500)))

    def test_unsettled_external_checks_mark_the_scan_incomplete(self):
        gh = ci.GitHub('example/project', full_history=True, snapshot='2026-11-05T00:00:00Z')
        def page(endpoint):
            if endpoint.startswith('actions/runs?'):
                return dict(total_count=0, workflow_runs=[])
            if endpoint.startswith('commits?'):
                return [dict(sha='a' * 40)]
            if '/check-suites?' in endpoint:
                return dict(total_count=1, check_suites=[dict(id=5, status='queued', created_at='2026-11-04T00:00:00Z')])
            return dict(total_count=1, check_runs=[dict(id=50, status='queued', conclusion=None, app=dict(slug='ci'))])
        with patch.object(gh, 'api', side_effect=page):
            ci.collect(gh, None)
        self.assertTrue(any('not completed' in reason for reason in gh.incomplete))

    def abandoned_suite_scan(self, created, updated=None):
        gh = ci.GitHub('example/project', full_history=True, snapshot='2026-11-05T00:00:00Z')
        gh.endpoints = []
        def page(endpoint):
            gh.endpoints.append(endpoint)
            if endpoint.startswith('actions/runs?'):
                return dict(total_count=0, workflow_runs=[])
            if endpoint.startswith('commits?'):
                return [dict(sha='a' * 40)]
            if '/check-suites?' in endpoint:
                return dict(total_count=1, check_suites=[dict(id=5, status='queued', latest_check_runs_count=0,
                                                              created_at=created, updated_at=updated or created)])
            return dict(total_count=0, check_runs=[])
        with patch.object(gh, 'api', side_effect=page):
            ci.collect(gh, None)
        return gh

    def test_long_queued_empty_suite_is_reported_without_blocking_completion_or_caching(self):
        gh = self.abandoned_suite_scan('2026-10-13T03:04:34Z')
        self.assertEqual((gh.incomplete, getattr(gh, 'abandoned', None)), ([], 1))
        self.assertTrue(any(e.startswith('check-suites/5/check-runs') and '&filter=all' in e for e in gh.endpoints))
        report = dict(counts=dict(failures_found=0, red_reproduced=0, proven=0, lessons_promoted=0, skipped=0),
                      approval_held=0, wall_seconds=0, enumeration_complete=True, repositories=[], repositories_selected=[],
                      api_requests=0, skipped_by_reason={}, incomplete_reasons=[], abandoned_check_suites=1)
        self.assertIn('1 queued check suites with no check runs', ci.report_markdown(report))
        with tempfile.TemporaryDirectory() as name:
            cache = Path(name) / 'cache'
            cache.mkdir(mode=0o700)
            body = json.dumps(dict(total_count=1, check_suites=[dict(id=5, status='queued', latest_check_runs_count=0,
                                                                  created_at='2026-10-13T03:04:34Z', updated_at='2026-10-13T03:04:34Z')]))
            response = b'HTTP/2.0 200 OK\r\nx-ratelimit-remaining: 2000\r\n\r\n' + body.encode()
            with patch.object(ci, 'run', return_value=response) as get:
                for _ in range(2):
                    ci.HistorySession(cache, remaining=2000, snapshot='2026-11-05T00:00:00Z').read(
                        'example/project', 'commits/' + 'a' * 40 + '/check-suites?per_page=100&page=1')
            self.assertEqual(get.call_count, 1)

    def test_recently_queued_empty_suite_still_marks_the_scan_incomplete(self):
        gh = self.abandoned_suite_scan('2026-11-04T12:00:00Z')
        self.assertTrue(any('not completed' in reason for reason in gh.incomplete))
        self.assertEqual(getattr(gh, 'abandoned', None), 0)

    def test_old_suite_requeued_recently_still_marks_the_scan_incomplete(self):
        gh = self.abandoned_suite_scan('2026-10-13T03:04:34Z', updated='2026-11-04T12:00:00Z')
        self.assertTrue(any('not completed' in reason for reason in gh.incomplete))
        self.assertEqual(getattr(gh, 'abandoned', None), 0)

    def test_stores_sharing_a_history_cache_share_its_snapshot(self):
        with tempfile.TemporaryDirectory() as name:
            cache = Path(name) / 'first' / '.coldfix' / 'shared-cache'
            snapshots = []
            for store in ('first', 'second'):
                args = argparse.Namespace(store=str(Path(name) / store / '.coldfix'), repo=['example/project'],
                    owner=None, max_failures=20, timeout=1, full_history=True, history_cache=str(cache))
                with patch.object(ci, 'run', return_value=b'{"resources":{"core":{"remaining":2000}}}'), patch.object(ci, 'GitHub', return_value=FakeGitHub([], {})), patch.object(ci, 'collect', return_value=[]), patch.object(ci.time, 'gmtime', return_value=__import__('time').gmtime(1762300800 + len(snapshots) * 86400)):
                    ci.execute(args)
                snapshots.append(json.loads((Path(args.store) / 'ci-checkpoints' / 'repositories.json').read_text())['snapshot'])
            self.assertEqual(snapshots[0], snapshots[1])

    def test_unfinished_jobs_are_never_frozen(self):
        jobs = dict(total_count=1, jobs=[dict(id=1, status='in_progress', conclusion=None)])
        self.assertEqual(self.reads('actions/runs/1/attempts/1/jobs?per_page=100&page=1', jobs), 2)

    def test_transient_proof_failure_is_retried_on_resume(self):
        with tempfile.TemporaryDirectory() as name:
            args = argparse.Namespace(store=str(Path(name) / '.coldfix'), repo=['example/project'],
                owner=None, max_failures=20, timeout=1, full_history=True, history_cache=None)
            gh = FakeGitHub([], {})
            failure = ci.Failure('workflow', workflow_run(1, 'a' * 40), {}, reason='workflow job enumeration unavailable')
            with patch.object(ci, 'run', return_value=b'{"resources":{"core":{"remaining":2000}}}'), patch.object(ci, 'GitHub', return_value=gh), patch.object(ci, 'collect', return_value=[failure]) as collect, patch.object(ci, 'attempt', side_effect=OSError):
                ci.execute(args)
                ci.execute(args)
            self.assertEqual(collect.call_count, 2)

    def test_transient_replay_skip_is_retried_on_resume(self):
        for reason in ('read operation unavailable', 'local tool unavailable'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as name:
                args = argparse.Namespace(store=str(Path(name) / '.coldfix'), repo=['example/project'],
                    owner=None, max_failures=20, timeout=1, full_history=True, history_cache=None)
                gh = FakeGitHub([], {})
                failure = ci.Failure('workflow', workflow_run(1, 'a' * 40), {}, reason='workflow job enumeration unavailable')
                skipped = dict(repo='example/project', source='workflow', links=[], status='SKIPPED',
                    reason=reason, red_reproduced=False, proven=False, lesson=None)
                with patch.object(ci, 'run', return_value=b'{"resources":{"core":{"remaining":2000}}}'), patch.object(ci, 'GitHub', return_value=gh), patch.object(ci, 'collect', return_value=[failure]) as collect, patch.object(ci, 'attempt', return_value=skipped):
                    ci.execute(args)
                    ci.execute(args)
                self.assertEqual(collect.call_count, 2)

    def test_historical_runner_startup_failure_is_checkpointed(self):
        with tempfile.TemporaryDirectory() as name:
            args = argparse.Namespace(store=str(Path(name) / '.coldfix'), repo=['example/project'],
                owner=None, max_failures=20, timeout=1, full_history=True, history_cache=None)
            reason = 'workflow has no executed failed job; runner or startup unavailable'
            failure = ci.Failure('workflow', workflow_run(1, 'a' * 40), {}, reason=reason)
            skipped = dict(repo='example/project', source='workflow', links=[], status='SKIPPED',
                reason=reason, red_reproduced=False, proven=False, lesson=None)
            with patch.object(ci, 'run', return_value=b'{"resources":{"core":{"remaining":2000}}}'), patch.object(ci, 'GitHub', return_value=FakeGitHub([], {})), patch.object(ci, 'collect', return_value=[failure]) as collect, patch.object(ci, 'attempt', return_value=skipped):
                ci.execute(args)
                report = ci.execute(args)
            self.assertEqual((collect.call_count, report['enumeration_complete']), (1, True))

    def test_promotions_survive_a_rescan_after_a_transient_failure(self):
        with tempfile.TemporaryDirectory() as name:
            args = argparse.Namespace(store=str(Path(name) / '.coldfix'), repo=['example/project'],
                owner=None, max_failures=20, timeout=1, full_history=True, history_cache=None)
            failures = [ci.Failure('workflow', workflow_run(n, 'a' * 40), {}, reason='workflow job enumeration unavailable') for n in (1, 2)]
            base = dict(repo='example/project', source='workflow', links=[], red_reproduced=True)
            transient = dict(base, status='SKIPPED', reason='read operation unavailable', proven=False, lesson=None)
            created = dict(base, status='PROVEN', reason=None, proven=True, lesson=dict(id='l' * 32, verification='proven', receipt_id='r' * 32))
            existing = dict(base, status='PROVEN', reason=None, proven=True, lesson=dict(status='existing', lesson='l' * 32, verification='proven'))
            reports = []
            for replies in ((created, transient), (existing, transient)):
                with patch.object(ci, 'run', return_value=b'{"resources":{"core":{"remaining":2000}}}'), patch.object(ci, 'GitHub', return_value=FakeGitHub([], {})), patch.object(ci, 'collect', return_value=failures), patch.object(ci, 'attempt', side_effect=list(replies)):
                    reports.append(json.loads(Path(ci.execute(args)['report']).read_text()))
            self.assertEqual([r['counts']['lessons_promoted'] for r in reports], [1, 1])

    def test_in_progress_run_marks_full_history_incomplete(self):
        gh = FakeGitHub([dict(workflow_run(1, 'a' * 40, None), status='in_progress')], {})
        gh.full_history = True
        ci.collect(gh, None)
        self.assertTrue(any('not completed' in reason for reason in gh.incomplete))

    def test_incomplete_repository_is_rescanned_on_resume(self):
        with tempfile.TemporaryDirectory() as name:
            args = argparse.Namespace(store=str(Path(name) / '.coldfix'), repo=['example/project'],
                owner=None, max_failures=20, timeout=1, full_history=True, history_cache=None)
            gh = FakeGitHub([], {})
            def partial(github, maximum):
                github.incomplete.append('workflow job enumeration unavailable; earlier results retained')
                return []
            with patch.object(ci, 'run', return_value=b'{"resources":{"core":{"remaining":2000}}}'), patch.object(ci, 'GitHub', return_value=gh), patch.object(ci, 'collect', side_effect=partial) as collect:
                ci.execute(args)
                ci.execute(args)
            self.assertEqual(collect.call_count, 2)

    def test_same_commit_and_job_passing_on_another_attempt_is_never_learned(self):
        red = workflow_run(1, 'a' * 40)
        retry = workflow_run(2, 'a' * 40, 'success', 2)
        green = workflow_run(3, 'b' * 40, 'success', 3)
        gh = FakeGitHub([red, retry, green], {1: [job()], 2: [job(20, 'success')], 3: [job(30, 'success')]})
        failure = ci.collect(gh, 20)[0]
        self.assertIsNone(failure.green)
        self.assertEqual(ci.reason_code(failure.reason), 'FLAKY_FAILURE')

    def test_explicit_flake_classification_never_selects_a_green_pair(self):
        red = dict(workflow_run(1, 'a' * 40), classification=dict(category='flake'))
        green = workflow_run(2, 'b' * 40, 'success', 2)
        gh = FakeGitHub([red, green], {1: [job()], 2: [job(20, 'success')]})
        failure = ci.collect(gh, 20)[0]
        self.assertIsNone(failure.green)
        self.assertEqual(ci.reason_code(failure.reason), 'FLAKY_FAILURE')
