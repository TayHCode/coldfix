import json
import fcntl
import http.server
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

import coldfix
import learn_candidates
import transcript_mistakes as tm

LOOPBACK = socket.inet_ntoa(socket.INADDR_LOOPBACK.to_bytes(4, 'big'))


def conversation(middle='The test failed; I fixed the import.'):
    return ('## assistant · 2026-01-01 00:00:00Z · a1\nI wrote the change.\n'
            '## system · 2026-01-01 00:00:01Z · b2\nPrivate system instructions.\n'
            f'## assistant · 2026-01-01 00:00:02Z · c3\n{middle}\n')


class TranscriptMistakesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / '2026-01-01_codex-example.md'
        self.path.write_text(conversation())
        self.ledger = self.root / 'ledger.sqlite'
        self.store = self.root / 'store'

    def scan(self):
        return tm.scan([self.path], self.ledger, self.store, 'http://example.invalid', 'jev', None, 10, 10, .6)

    def scan_native(self, path, text):
        path.parent.mkdir(parents=True)
        path.write_text('\n'.join(json.dumps(line) for line in text) + '\n{"truncated":\n')
        self.assertEqual(tm.transcripts([self.root]), [path])
        with patch.object(tm, 'score', return_value=.9):
            result = tm.scan(tm.transcripts([self.root]), self.ledger, self.store, 'http://example.invalid', 'jev', None, 10, 10, .6)
        self.assertEqual((result['scanned'], result['jev_flagged'], result['imported'], result['errors']), (1, 1, 1, 0))
        [record] = [json.loads(p.read_text()) for p in (self.store / 'failures').glob('*.json')]
        return record

    def test_claude_code_project_jsonl_is_ingested_without_coldvault(self):
        self.path.unlink()
        record = self.scan_native(self.root / '.claude/projects/-home-u-repo/5f0c.jsonl', [
            {'type': 'summary', 'summary': 'ignored'},
            {'type': 'user', 'uuid': 'u-1', 'timestamp': '2026-01-01T00:00:00Z', 'message': {'role': 'user', 'content': 'Add the parser.'}},
            {'type': 'assistant', 'uuid': 'a-1', 'timestamp': '2026-01-01T00:00:01Z', 'message': {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': 'hidden reasoning'}, {'type': 'text', 'text': 'I wrote the parser.'}]}},
            {'type': 'user', 'uuid': 'u-2', 'timestamp': '2026-01-01T00:00:02Z', 'isMeta': True, 'message': {'role': 'user', 'content': 'meta wrong'}},
            {'type': 'user', 'uuid': 'u-3', 'timestamp': '2026-01-01T00:00:03Z', 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'content': 'tool output'}, {'type': 'text', 'text': 'That is wrong: the test failed.'}]}},
            {'type': 'assistant', 'uuid': 'a-2', 'timestamp': '2026-01-01T00:00:04Z', 'message': {'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'Fixed the import.'}]}}])
        self.assertEqual(record['agent'], 'claude')
        self.assertEqual(record['occurred'], '2026-01-01T00:00:04Z')
        self.assertTrue(record['source_ref'].endswith('5f0c.jsonl#messages=a-1,u-3,a-2'))
        evidence = json.loads(record['error'])['evidence']
        self.assertIn('That is wrong: the test failed.', evidence)
        for hidden in ('hidden reasoning', 'tool output', 'meta wrong', 'ignored'):
            self.assertNotIn(hidden, evidence)

    def test_codex_session_rollout_is_ingested_without_coldvault(self):
        self.path.unlink()
        record = self.scan_native(self.root / '.codex/sessions/2026/01/01/rollout-2026-01-01T00-00-00-abc.jsonl', [
            {'timestamp': '2026-01-01T00:00:00Z', 'type': 'session_meta', 'payload': {'id': 'abc'}},
            {'timestamp': '2026-01-01T00:00:01Z', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer', 'content': [
                {'type': 'input_text', 'text': 'developer error rules'}]}},
            {'timestamp': '2026-01-01T00:00:02Z', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': 'I wrote the change.'}]}},
            {'timestamp': '2026-01-01T00:00:03Z', 'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'call error'}},
            {'timestamp': '2026-01-01T00:00:04Z', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'The build failed.'}]}},
            {'timestamp': '2026-01-01T00:00:05Z', 'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'duplicate error'}},
            {'timestamp': '2026-01-01T00:00:06Z', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': 'Fixed the build.'}]}},
            {'timestamp': 'not-a-date', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': 'Bad timestamp error.'}]}}])
        self.assertEqual(record['agent'], 'codex')
        self.assertEqual(record['occurred'], '2026-01-01T00:00:06Z')
        self.assertTrue(record['source_ref'].endswith('#messages=L3,L5,L7'))
        evidence = json.loads(record['error'])['evidence']
        self.assertIn('user [L5]: The build failed.', evidence)
        for hidden in ('developer error rules', 'call error', 'duplicate error', 'Bad timestamp error'):
            self.assertNotIn(hidden, evidence)

    def test_chatgpt_conversations_are_backlog_and_index_pages_are_not(self):
        chat = self.root / '2024-01-01_chatgpt-example.md'
        chat.write_text('## mixed · 2024-01-01 00:00:00Z · m1\nPasted notes.\n' + conversation())
        (self.root / '_Index.md').write_text('## Index\n')
        self.assertEqual(tm.transcripts([self.root]), sorted([self.path, chat]))
        self.assertEqual(tm.messages(chat.read_text())[0]['role'], 'mixed')
        self.assertEqual(tm.event(chat, tm.messages(chat.read_text()), .9)['agent'], 'chatgpt')

    def test_flagged_transcript_is_ingested_once_with_message_evidence(self):
        with patch.object(tm, 'score', return_value=.9) as call:
            first = self.scan()
            second = self.scan()
        self.assertEqual((first['scanned'], first['jev_flagged'], first['imported']), (1, 1, 1))
        self.assertEqual(second['scanned'], 0)
        self.assertEqual(call.call_count, 1)
        records = list((self.store / 'failures').glob('*.json'))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertIn('#messages=a1,c3', record['source_ref'])
        self.assertIn('fixed the import', record['error'])
        self.assertEqual(json.loads(record['error'])['score'], .9)
        self.assertNotIn('Private system instructions', record['error'])

    def test_failed_jev_call_is_retried_instead_of_marked_scanned(self):
        with patch.object(tm, 'score', side_effect=ValueError('invalid response')):
            self.assertEqual(self.scan()['errors'], 1)
        with patch.object(tm, 'score', return_value=.1) as call:
            self.assertEqual(self.scan()['scanned'], 1)
        self.assertEqual(call.call_count, 1)
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(db.execute('select status from scans').fetchone()[0], 'scored')

    def test_failed_ingestion_reuses_the_saved_score(self):
        with patch.object(tm, 'score', return_value=.95) as score, patch.object(tm, 'ingest', side_effect=OSError('interrupted')):
            self.assertEqual(self.scan()['errors'], 1)
        self.assertEqual(score.call_count, 1)
        with patch.object(tm, 'score') as score:
            result = self.scan()
        score.assert_not_called()
        self.assertEqual((result['jev_calls'], result['imported']), (0, 1))
        self.assertEqual(len(list((self.store / 'failures').glob('*.json'))), 1)

    def test_interruption_after_ingestion_does_not_duplicate_the_event(self):
        ingest = tm.ingest
        def interrupted(*args):
            ingest(*args)
            raise OSError('interrupted')
        with patch.object(tm, 'score', return_value=.95), patch.object(tm, 'ingest', side_effect=interrupted):
            self.assertEqual(self.scan()['errors'], 1)
        with patch.object(tm, 'score') as score:
            result = self.scan()
        score.assert_not_called()
        self.assertEqual(result['imported'], 0)
        self.assertEqual(len(list((self.store / 'failures').glob('*.json'))), 1)

    def test_staged_input_keeps_the_original_source_identity(self):
        original = Path('/vault/Conversations/Codex/session_codex-example.md')
        with patch.object(tm, 'score', return_value=.9):
            tm.scan([self.path], self.ledger, self.store, 'http://example.invalid', 'jev', None, 10, 10, .6,
                    source_map=lambda _: original)
        record = json.loads(next((self.store / 'failures').glob('*.json')).read_text())
        self.assertTrue(record['source_ref'].startswith('transcript:' + str(original) + '#messages='))
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(db.execute('select path from scans').fetchone()[0], str(original))

    def test_changed_transcript_is_rescanned(self):
        with patch.object(tm, 'score', return_value=.1) as call:
            self.scan()
            self.path.write_text(conversation('A bug was fixed in the assertion.'))
            self.scan()
        self.assertEqual(call.call_count, 2)

    def test_new_ledger_is_private_with_permissive_umask(self):
        previous = os.umask(0)
        try:
            with patch.object(tm, 'score', return_value=.1):
                self.scan()
        finally:
            os.umask(previous)
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)

    def test_concurrent_scan_cannot_rescore_the_same_transcript(self):
        self.ledger.touch()
        with self.ledger.open('r+') as ledger:
            fcntl.flock(ledger, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(tm, 'score') as call, self.assertRaises(BlockingIOError):
                self.scan()
            call.assert_not_called()

    def test_ledger_rejects_a_different_store_or_scorer(self):
        with patch.object(tm, 'score', return_value=.1):
            self.scan()
        with self.assertRaisesRegex(ValueError, 'different store or scorer'):
            tm.scan([self.path], self.ledger, self.root / 'other', 'http://example.invalid', 'jev', None, 10, 10, .6)
        with self.assertRaisesRegex(ValueError, 'different store or scorer'):
            tm.scan([self.path], self.ledger, self.store, 'http://example.invalid', 'other-model', None, 10, 10, .6)

    def test_single_strong_transcript_becomes_a_candidate_but_weak_one_does_not(self):
        with patch.object(tm, 'score', return_value=.6):
            self.scan()
        self.path.write_text(conversation('Another bug was fixed in the assertion.'))
        with patch.object(tm, 'score', return_value=.59):
            self.scan()
        candidates = learn_candidates.candidates(coldfix.load_store(self.store)[1])
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]['transcript'])
        self.assertEqual(len(candidates[0]['evidence']), 1)

    def test_no_correction_language_never_calls_jev(self):
        self.path.write_text(conversation('The requested greeting is ready.'))
        with patch.object(tm, 'score') as call:
            result = self.scan()
        self.assertEqual(result['prescreened'], 0)
        call.assert_not_called()

    def test_wrong_guesses_and_repeats_across_helpers_are_flagged_and_labelled(self):
        for middle, kind in (('I assumed the port was 8080, but it turns out to be 9090.', 'guess'),
                             ('You did this again, just like the other agent.', 'repeat')):
            with self.subTest(kind=kind):
                self.path.write_text(conversation(middle))
                self.assertEqual(tm.CORRECTION.search(middle), None)
                with patch.object(tm, 'score', return_value=.9):
                    result = self.scan()
                self.assertEqual((result['prescreened'], result[kind]), (1, 1))
                errors = [json.loads(json.loads(p.read_text())['error']) for p in (self.store / 'failures').glob('*.json')]
                [error] = [error for error in errors if middle in error['evidence']]
                self.assertEqual(error['mistake_types'], [kind])

    def test_a_new_prescreen_reopens_transcripts_the_old_one_screened_out(self):
        self.path.write_text(conversation('I guessed the port.'))
        with patch.object(tm, 'PRESCREEN', 'old'), patch.object(tm, 'MISTAKE_TYPES', dict(correction=tm.CORRECTION)):
            self.assertEqual(self.scan()['prescreened'], 0)
            self.assertEqual(self.scan()['scanned'], 0)
        with patch.object(tm, 'score', return_value=.1) as call:
            self.assertEqual(self.scan()['guess'], 1)
            self.assertEqual(self.scan()['scanned'], 0)
        self.assertEqual(call.call_count, 1)
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(db.execute("select rowid > (select cast(value as integer) from settings where name='prescreen_rowid') from scans").fetchone(), (1,))

    def test_missing_transcript_root_fails_instead_of_reporting_empty_success(self):
        result = subprocess.run([sys.executable, str(Path(tm.__file__)), str(self.root / 'missing'),
                                 '--ledger', str(self.ledger), '--store', str(self.store),
                                 '--endpoint', 'https://example.invalid', '--limit', '1', '--max-calls', '1'],
                                env=dict(os.environ, JEV_API_KEY='secret'), text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('existing directory', result.stderr)

    def test_unrelated_repository_links_are_not_assigned_to_the_event(self):
        rows = tm.messages(conversation('Fixed. https://github.com/a/one/pull/1 https://github.com/b/two/pull/2'))
        result = tm.event(self.path, tm.excerpt(rows), .9)
        self.assertEqual(result['repo'], 'unknown')
        self.assertEqual(result['thread'], '')

    def test_excerpt_withholds_credentials_from_the_judge(self):
        token = 'ghp_' + 'a' * 36
        value = 'hunter' + '2xyz'
        rows = tm.messages(conversation('Fixed. I used ' + token + ' and pass' + 'word=' + value))
        state = json.dumps(tm.excerpt(rows))
        self.assertNotIn(token, state)
        self.assertNotIn(value, state)
        self.assertIn('[redacted]', state)

    def test_excerpt_withholds_a_whole_private_key_block(self):
        body = 'b3BlbnNzaC1rZXktdjEAAAAABG5vbmU' + 'Q' * 40
        label = 'OPENSSH ' + 'PRIVATE KEY-----'
        for end in ('\n-----END ' + label + '\nthanks', ''):
            with self.subTest(complete=bool(end)):
                key = '-----BEGIN ' + label + '\n' + body + end
                state = json.dumps(tm.excerpt(tm.messages(conversation('Fixed. I used ' + key))))
                self.assertNotIn(body, state)
                self.assertIn('[redacted]', state)

    def test_excerpt_withholds_secret_blocks_in_every_shape(self):
        body = 'cGF5bG9hZA' + 'Z' * 40
        dash = '-' * 5

        def block(kind, label):
            return dash + kind + ' ' + label + ' ' + 'PRIVATE KEY' + (' BLOCK' if label == 'PGP' else '') + dash

        cases = {
            'pgp': ['Fixed. ' + block('BEGIN', 'PGP') + '\n' + body + '\n' + block('END', 'PGP') + ' done'],
            'crlf indented': ['Fixed.\r\n    > ' + block('BEGIN', 'RSA') + '\r\n    > ' + body + '\r\n    > ' + block('END', 'RSA')],
            'several': ['Fixed. ' + block('BEGIN', 'EC') + '\nx\n' + block('END', 'EC') + ' and '
                        + block('BEGIN', 'DSA') + '\n' + body + '\n' + block('END', 'DSA')],
            'mismatched end': ['Fixed. ' + block('BEGIN', 'OPENSSH') + '\nx\n' + block('END', 'RSA') + '\n' + body],
            'across rows': ['Fixed. ' + block('BEGIN', 'OPENSSH') + '\nfirst', body + '\n' + block('END', 'OPENSSH') + ' ok'],
            'yaml block': ['Fixed. config:\n  api_' + 'key: |\n    ' + body + '\n    more\nnext: 1'],
        }
        for name, texts in cases.items():
            with self.subTest(name):
                rows = [dict(role='assistant', text='I wrote the change.')] + [
                    dict(role='assistant' if i else 'user', text=text) for i, text in enumerate(texts)]
                state = json.dumps(tm.excerpt(rows))
                self.assertIn('[redacted]', state)
                self.assertNotIn(body, state)

    def test_repeated_identical_link_is_retained_as_evidence(self):
        url = 'https://github.com/a/one/pull/1'
        rows = tm.messages(conversation('Fixed. ' + url + ' ' + url))
        result = tm.event(self.path, tm.excerpt(rows), .9)
        self.assertEqual((result['repo'], result['thread']), ('a/one', url))

    def test_rate_limited_call_is_recorded_and_can_be_retried(self):
        class Limited(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(429)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer((LOOPBACK, 0), Limited)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://{LOOPBACK}:{server.server_port}'
        jev = lambda *args: tm.jev(*args, url, 'jev', 'secret')
        result = tm.scan([self.path], self.ledger, self.store, url, 'jev', jev, 1, 1, .6)
        self.assertEqual(result['error'], 'http_429')
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(db.execute('select count(*) from scans').fetchone()[0], 0)
            self.assertEqual(db.execute('select status from attempts').fetchone()[0], 'http_429')
        with patch.object(tm, 'score', return_value=.1):
            retried = tm.scan([self.path], self.ledger, self.store, url, 'jev', jev, 1, 1, .6)
        self.assertEqual(retried['scanned'], 1)

    def test_short_rate_limit_uses_bounded_backoff(self):
        limited = urllib.error.HTTPError('https://example.invalid', 429, 'limited', {'Retry-After': '3'}, None)
        with patch.object(tm, 'score', side_effect=[limited, .1]), patch.object(tm.time, 'sleep') as sleep:
            result = self.scan()
        self.assertEqual(result['jev_calls'], 2)
        self.assertEqual(result['errors'], 0)
        sleep.assert_called_once_with(3)
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(db.execute('select status from attempts order by rowid').fetchall(), [('http_429',), ('completed',)])

    def test_http_date_rate_limit_stops_without_retrying_early(self):
        reset = datetime.now(timezone.utc) + timedelta(minutes=10)
        limited = urllib.error.HTTPError('https://example.invalid', 429, 'limited',
                                         {'Retry-After': format_datetime(reset, usegmt=True)}, None)
        with patch.object(tm, 'score', side_effect=limited) as score, patch.object(tm.time, 'sleep') as sleep:
            result = self.scan()
        self.assertEqual(result['error'], 'http_429')
        self.assertGreaterEqual(result['retry_after_seconds'], 590)
        score.assert_called_once()
        sleep.assert_not_called()

    def test_short_http_date_is_converted_to_a_delay(self):
        now = datetime(2026, 11, 6, tzinfo=timezone.utc)
        reset = format_datetime(now + timedelta(seconds=3), usegmt=True)
        self.assertEqual(tm.retry_after_seconds(reset, now), 3)
        self.assertEqual(tm.retry_after_seconds('Fri Nov  6 00:00:03 2026', now), 3)

    def test_large_numeric_retry_after_is_not_treated_as_a_short_limit(self):
        now = datetime(2026, 11, 6, tzinfo=timezone.utc)
        self.assertEqual(tm.retry_after_seconds('10000000000', now), 10000000000)

    def test_the_model_command_judges_in_place_of_jev(self):
        stub = self.root / 'judge.py'
        stub.write_text('import json, sys\nsys.stdin.read()\nprint(json.dumps(dict(score=float(sys.argv[1]))))\n'
                        'open(sys.argv[0] + ".role", "w").write(sys.argv[2])\n')
        with patch.dict(os.environ, COLDFIX_MODEL_CMD=f'{sys.executable} {stub} placeholder'):
            os.environ.pop('JEV_API_KEY', None)
            call, label = tm.judge('https://jev.invalid', 'jev-latest')
            self.assertRegex(label, '^COLDFIX_MODEL_CMD judge [0-9a-f]{12}$')
            os.environ['COLDFIX_MODEL_CMD'] = f'{sys.executable} {stub} other'
            self.assertNotEqual(tm.judge('https://jev.invalid', 'jev-latest')[1], label)
            for value in ('0.7', '1.5'):
                os.environ['COLDFIX_MODEL_CMD'] = f'{sys.executable} {stub} {value}'
                with self.subTest(score=value):
                    if value == '1.5':
                        with self.assertRaises(ValueError):
                            tm.score([], call)
                    else:
                        self.assertEqual(tm.score([], call), .7)
        self.assertEqual((self.root / 'judge.py.role').read_text(), 'judge')
        with patch.dict(os.environ, {'COLDFIX_JUDGE': 'jev'}, clear=True):
            self.assertEqual(tm.judge('https://jev.invalid', 'jev-latest'), (None, 'jev-latest'))

    def test_the_bulk_role_judges_first_even_with_a_jev_key(self):
        haiku = dict(provider='claudeAgent', model='claude-haiku-5-5', effort='default')
        asked = []

        def ask(model, prompt, shape, work):
            asked.append(model['model'])
            return dict(score=.8)
        with patch.dict(os.environ, JEV_API_KEY='secret'), \
                patch.object(tm, 'jev', side_effect=AssertionError('Jev called')), \
                patch.object(tm.learn_daily, 'route', side_effect=lambda role: dict(haiku, role=role)) as route, \
                patch.object(tm.learn_daily, 'ask', side_effect=ask):
            for name in ('COLDFIX_MODEL_CMD', 'COLDFIX_JUDGE'):
                os.environ.pop(name, None)
            call, label = tm.judge('https://jev.invalid', 'jev-latest')
            self.assertEqual([tm.score([], call), tm.score([], call)], [.8, .8])
        route.assert_called_once_with('bulk')
        self.assertEqual((label, call.model, getattr(call, 'fallen_back', False)), ('jev-latest', 'claude-haiku-5-5', False))
        self.assertEqual(asked, ['claude-haiku-5-5'] * 2)

    def test_an_unreachable_jev_hands_the_rest_of_the_run_to_the_bulk_role(self):
        with socket.socket() as probe:
            probe.bind((LOOPBACK, 0))
            closed = f'http://{LOOPBACK}:{probe.getsockname()[1]}/v1'
        haiku = dict(provider='claudeAgent', model='claude-haiku-5-5', effort='default')
        asked = []

        def ask(model, prompt, shape, work):
            asked.append((model, prompt, shape))
            return dict(score=.8)
        with patch.dict(os.environ, JEV_API_KEY='secret', COLDFIX_JUDGE='jev'), \
                patch.object(tm.learn_daily, 'route', side_effect=lambda role: dict(haiku, role=role)) as route, \
                patch.object(tm.learn_daily, 'ask', side_effect=ask):
            os.environ.pop('COLDFIX_MODEL_CMD', None)
            call, label = tm.judge(closed, 'jev-latest')
            self.assertEqual((label, call.model), ('jev-latest', 'jev-latest'))
            self.assertEqual([tm.score([], call), tm.score([], call)], [.8, .8])
        route.assert_called_once_with('bulk')
        self.assertEqual((call.model, call.fallen_back), ('claude-haiku-5-5', True))
        self.assertEqual([(model['model'], shape) for model, _, shape in asked], [('claude-haiku-5-5', tm.SCORE)] * 2)
        self.assertTrue(asked[0][1].startswith(tm.QUESTION))

    def test_a_jev_server_error_falls_back_but_a_rate_limit_keeps_jev_and_its_retry(self):
        for code, fallen in ((429, False), (401, False), (503, True)):
            def refused(*args, code=code):
                raise urllib.error.HTTPError('https://jev.invalid', code, 'no', {}, None)
            call = tm.JevOrRole(refused, 'jev-latest')
            with self.subTest(code=code), patch.object(tm, 'role_judge', return_value=(lambda *a: .3, 'claude-haiku-5-5')):
                if fallen:
                    self.assertEqual(tm.score([], call), .3)
                else:
                    with self.assertRaises(urllib.error.HTTPError):
                        tm.score([], call)
                self.assertEqual(call.fallen_back, fallen)

    def test_a_fallback_score_is_rescored_once_jev_answers_again(self):
        def down(*args):
            raise OSError('jev down')
        scan = lambda judge: tm.scan([self.path], self.ledger, self.store, 'http://example.invalid', 'jev', judge, 10, 10, .6)
        status = lambda: sqlite3.connect(self.ledger).execute('select status, score from scans').fetchone()
        with patch.object(tm, 'role_judge', return_value=(lambda *a: .1, 'claude-haiku-5-5')):
            self.assertEqual(scan(tm.JevOrRole(down, 'jev'))['scanned'], 1)
        self.assertEqual(status(), ('fallback_scored', .1))
        fallen = tm.JevOrRole(down, 'jev')
        with patch.object(tm, 'role_judge', return_value=(lambda *a: .1, 'claude-haiku-5-5')):
            fallen({}, 'warm', '', {})
            self.assertEqual(scan(fallen)['scanned'], 0)
        self.assertEqual(scan(tm.JevOrRole(lambda *a: .2, 'jev'))['scanned'], 1)
        self.assertEqual(status(), ('scored', .2))
        self.assertEqual(scan(tm.JevOrRole(lambda *a: .3, 'jev'))['scanned'], 0)

    def test_a_cached_fallback_score_waits_behind_new_transcripts_while_jev_stays_down(self):
        def down(*args):
            raise OSError('jev down')
        later = self.root / '2026-01-02_codex-example.md'
        scan = lambda paths: tm.scan(paths, self.ledger, self.store, 'http://example.invalid', 'jev', tm.JevOrRole(down, 'jev'), 10, 1, .6)
        with patch.object(tm, 'role_judge', return_value=(lambda *a: .1, 'claude-haiku-5-5')):
            scan([self.path])
            later.write_text(conversation())
            self.assertEqual(scan([self.path, later])['jev_calls'], 1)
        rows = dict(sqlite3.connect(self.ledger).execute('select path, status from scans'))
        self.assertEqual(rows, {str(self.path.resolve()): 'fallback_scored', str(later.resolve()): 'fallback_scored'})

    def test_jev_stays_the_judge_while_it_answers_and_a_bad_score_is_not_a_fallback(self):
        replies = iter([.7, 2.0])
        call = tm.JevOrRole(lambda *args: tm.probability(next(replies)), 'jev-latest')
        with patch.object(tm.learn_daily, 'route') as route:
            self.assertEqual(tm.score([], call), .7)
            with self.assertRaises(ValueError):
                tm.score([], call)
        route.assert_not_called()
        self.assertEqual(call.model, 'jev-latest')

    def test_a_timed_out_model_command_is_a_retryable_scan_error(self):
        with patch.object(tm, 'score', side_effect=subprocess.TimeoutExpired('model', 1800)):
            result = self.scan()
        self.assertEqual((result['errors'], result['error'], result['scanned']), (1, 'TimeoutExpired', 1))
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(db.execute('select count(*) from scans').fetchone()[0], 0)

    def test_jev_http_response_drives_ingestion_and_candidate(self):
        class Jev(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.server.seen = (self.headers['Authorization'], request)
                body = json.dumps({'answers': {'corrected_mistake': {'noul': .95}}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer((LOOPBACK, 0), Jev)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://{LOOPBACK}:{server.server_port}'
        jev = lambda *args: tm.jev(*args, url, 'jev-test', 'secret')
        result = tm.scan([self.path], self.ledger, self.store, url, 'jev-test', jev, 1, 1, .6)
        self.assertEqual((result['jev_calls'], result['imported']), (1, 1))
        self.assertEqual(server.seen[0], 'Bearer secret')
        self.assertEqual(server.seen[1]['model'], 'jev-test')
        self.assertEqual(len(learn_candidates.candidates(coldfix.load_store(self.store)[1])), 1)


if __name__ == '__main__':
    unittest.main()
