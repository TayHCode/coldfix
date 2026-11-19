import io
import itertools
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

import collect_sources
import coldfix


def settled_jobs(jobs):
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w") as archive:
        for identifier, job in jobs.items():
            for name, content in [(identifier + ".json", json.dumps(job).encode()),
                                  (identifier + ".log", b"guest failed")]:
                info = tarfile.TarInfo(name)
                info.size, info.mtime = len(content), time.time() - 120
                archive.addfile(info, io.BytesIO(content))
    return bundle.getvalue()


class SourceTests(unittest.TestCase):
    def test_receipt_and_unavailable_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            store = work / 'store'
            store.mkdir(mode=0o700)
            receipt = dict(schema='coldbox.receipt/v1', id='cbx-1700000000-a1b2c3', mode='runner',
                           repo='example/app', sha='a' * 40, verdict='fail', started_at='2026-11-01T12:00:00Z',
                           checks=[dict(check='unit', command='unittest', status='fail', exit=1)])
            bundle = work / 'evidence.tar'
            payload = json.dumps(receipt).encode()
            with tarfile.open(bundle, 'w') as archive:
                info = tarfile.TarInfo('cbx-1700000000-a1b2c3.json')
                info.size, info.mtime = len(payload), time.time() - 120
                archive.addfile(info, io.BytesIO(payload))
            config = work / 'sources.json'
            config.write_text(json.dumps({'runner': {'format': 'coldbox-tar', 'command': ['cat', str(bundle)]},
                                          'vm': {'format': 'failure-json', 'command': [sys.executable, '-c', 'raise SystemExit(2)']}}))
            first = collect_sources.collect(store, config)
            self.assertEqual(first['runner']['imported'], 1)
            self.assertEqual(first['vm']['unavailable'], 1)
            self.assertEqual(len(coldfix.load_store(store)[1]['failures']), 1)
            second = collect_sources.collect(store, config)
            self.assertEqual(second['runner']['imported'], 0)
            self.assertEqual(second['runner']['deduped'], 1)
            self.assertEqual(second['vm']['unavailable'], 1)
            with tarfile.open(bundle, 'w') as archive:
                info = tarfile.TarInfo('other.json')
                info.size, info.mtime = len(payload), time.time() - 120
                archive.addfile(info, io.BytesIO(payload))
            renamed = collect_sources.collect(store, config)
            self.assertEqual(renamed['runner']['imported'], 0)
            self.assertEqual(renamed['runner']['deduped'], 1)

    def test_standby_host_sources_import_failures_and_accept_canceled(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            store = work / 'store'
            store.mkdir(mode=0o700)
            sources = {}
            for index, host in enumerate(('edge-b2', 'edge-c3')):
                jobs = {}
                for offset, verdict in enumerate(('fail', 'canceled')):
                    identifier = f'cbx-{1794474472 + 2 * index + offset}-c9b0b6-{host}'
                    jobs[identifier] = dict(schema='coldbox.receipt/v1', id=identifier, mode='runner', host=host,
                                            repo='example/app', sha='a' * 40, verdict=verdict,
                                            started_at='2026-11-12T10:00:00Z', job_result=verdict.title(),
                                            checks=[dict(check='unit', command='unittest', status='fail', exit=1)]
                                            if verdict == 'fail' else [])
                bundle = work / f'{host}.tar'
                bundle.write_bytes(settled_jobs(jobs))
                sources[host] = {'format': 'coldbox-tar', 'command': ['cat', str(bundle)]}
            config = work / 'sources.json'
            config.write_text(json.dumps(sources))
            result = collect_sources.collect(store, config)
            for host in ('edge-b2', 'edge-c3'):
                with self.subTest(host=host):
                    self.assertEqual({key: result[host][key] for key in ('imported', 'rejected', 'unavailable')},
                                     dict(imported=1, rejected=0, unavailable=0))
            self.assertEqual(sorted(json.loads(record['error'])['id']
                                    for record in coldfix.load_store(store)[1]['failures'].values()),
                             ['cbx-1794474472-c9b0b6-edge-b2', 'cbx-1794474474-c9b0b6-edge-c3'])

    def test_receipt_rejected_by_an_older_parser_is_imported_not_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            store = work / 'store'
            store.mkdir(mode=0o700)
            receipt = dict(schema='coldbox.receipt/v1', id='cbx-1700000000-a1b2c3', mode='runner',
                           repo='example/app', sha='a' * 40, verdict='fail', started_at='2026-11-01T12:00:00Z',
                           checks=[dict(check='unit', command='unittest', status='fail', exit=1)])
            bundle = work / 'evidence.tar'
            payload = json.dumps(receipt).encode()
            with tarfile.open(bundle, 'w') as archive:
                info = tarfile.TarInfo('cbx-1700000000-a1b2c3.json')
                info.size, info.mtime = len(payload), time.time() - 120
                archive.addfile(info, io.BytesIO(payload))
            (store / 'collected-receipts.json').write_text(
                json.dumps({'cbx-1700000000-a1b2c3.json': 'invalid coldbox receipt; nothing imported'}))
            config = work / 'sources.json'
            config.write_text(json.dumps({'runner': {'format': 'coldbox-tar', 'command': ['cat', str(bundle)]}}))
            result = collect_sources.collect(store, config)['runner']
            self.assertEqual((result['imported'], result['rejected'], result['rejection_reasons']), (1, 0, {}))
            self.assertEqual(len(coldfix.load_store(store)[1]['failures']), 1)

    def test_invalid_receipt_is_reported_as_known_invalid_on_later_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            store = work / 'store'
            store.mkdir(mode=0o700)
            receipt = dict(schema='coldbox.receipt/v1', id='cbx-1700000000-a1b2c3', mode='runner',
                           repo='stress', sha='a' * 40, verdict='fail', started_at='2026-11-01T12:00:00Z',
                           checks=[dict(check='unit', command='unittest', status='fail', exit=1)])
            bundle = work / 'evidence.tar'
            payload = json.dumps(receipt).encode()
            with tarfile.open(bundle, 'w') as archive:
                info = tarfile.TarInfo('cbx-1700000000-a1b2c3.json')
                info.size, info.mtime = len(payload), time.time() - 120
                archive.addfile(info, io.BytesIO(payload))
            config = work / 'sources.json'
            config.write_text(json.dumps({'runner': {'format': 'coldbox-tar', 'command': ['cat', str(bundle)]}}))
            first = collect_sources.collect(store, config)['runner']
            self.assertEqual((first['rejected'], first['known_invalid']), (1, 0))
            self.assertEqual(first['rejection_reasons'], {'coldbox receipt repo must be owner/repo; nothing imported': 1})
            again = collect_sources.collect(store, config)['runner']
            self.assertEqual((again['imported'], again['deduped'], again['rejected'], again['known_invalid'], again['rejection_reasons']),
                             (0, 0, 0, 1, {}))

    def test_vm02_finished_jobs_are_imported_once(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            store = work / "store"
            store.mkdir(mode=0o700)
            identifier = "00000000-0000-4000-8000-000000000001"
            job = dict(token=identifier, version="vm02-lifecycle-v2", verdict="FAIL", cleanup="gone",
                       cleaned_at=1793865073, started=1793865033, owner="coldfix", command=["pytest"], guest_exit=1)
            bundle = work / "jobs.tar"
            with tarfile.open(bundle, "w") as archive:
                for name, content in [(identifier + ".json", json.dumps(job).encode()),
                                      (identifier + ".log", b"AssertionError: expected 1")]:
                    info = tarfile.TarInfo(name)
                    info.size, info.mtime = len(content), time.time()
                    archive.addfile(info, io.BytesIO(content))
            config = work / "sources.json"
            config.write_text(json.dumps({"vm-b": {"format": "vm02-tar", "command": ["cat", str(bundle)]}}))
            unsettled = collect_sources.collect(store, config)["vm-b"]
            self.assertEqual((unsettled["imported"], unsettled["unsettled"]), (0, 1))
            with tarfile.open(bundle, "w") as archive:
                for name, content in [(identifier + ".json", json.dumps(job).encode()),
                                      (identifier + ".log", b"AssertionError: expected 1")]:
                    info = tarfile.TarInfo(name)
                    info.size, info.mtime = len(content), time.time() - 120
                    archive.addfile(info, io.BytesIO(content))
            first = collect_sources.collect(store, config)["vm-b"]
            second = collect_sources.collect(store, config)["vm-b"]
            self.assertEqual((first["imported"], first["rejected"]), (1, 0))
            self.assertEqual((second["imported"], second["deduped"]), (0, 1))
            record = next(iter(coldfix.load_store(store)[1]["failures"].values()))
            self.assertEqual((record["repo"], record["command"]), ("unknown", "pytest"))
            self.assertIn("AssertionError: expected 1", record["error"])

    def test_replay_evidence_distinguishes_vm_wrapper_from_coldbox_test(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / "store"
            store.mkdir(mode=0o700)
            identifier = "00000000-0000-4000-8000-000000000001"
            job = dict(token=identifier, version="vm02-lifecycle-v2", verdict="FAIL", cleanup="gone",
                       cleaned_at=1793865073, started=1793865033, owner="coldfix",
                       command=["sh", "-ec", "python3 /tmp/guest.py"], guest_exit=1)
            bundle = io.BytesIO()
            with tarfile.open(fileobj=bundle, mode="w") as archive:
                for name, content in [(identifier + ".json", json.dumps(job).encode()),
                                      (identifier + ".log", b"guest failed")]:
                    info = tarfile.TarInfo(name)
                    info.size, info.mtime = len(content), time.time() - 120
                    archive.addfile(info, io.BytesIO(content))
            self.assertEqual(collect_sources.vm02_jobs(store, bundle.getvalue())["imported"], 1)
            wrapper, = coldfix.load_store(store)[1]["failures"].values()
            self.assertEqual((wrapper["repo"], wrapper["source"]), ("unknown", "vm02-jobs"))
            self.assertNotIn("command", json.loads(wrapper["error"]))
            receipt = dict(schema="coldbox.receipt/v1", id="cbx-1700000000-a1b2c3", mode="local",
                           repo="example/app", sha="a" * 40, verdict="fail", started_at="2026-11-01T12:00:00Z",
                           checks=[dict(check="unit", command="unittest", status="fail", exit=1)],
                           tests=dict(results=[dict(suite="unit", classname="pkg.TestCount",
                                                    name="test_count", status="failed")]))
            failure, = coldfix.coldbox_events(receipt, Path(folder) / "receipt.json")
            self.assertIn("FAIL: test_count", failure["error"])

    def test_vm02_receipt_identity_is_read_when_present(self):
        base = dict(version="vm02-lifecycle-v2", verdict="FAIL", cleanup="gone", cleaned_at=1793865073,
                    started=1793865033, owner="coldfix", command=["pytest"], guest_exit=1)
        new = "00000000-0000-4000-8000-000000000002"
        old = "00000000-0000-4000-8000-000000000003"
        unfinished = "00000000-0000-4000-8000-000000000004"
        blank = "00000000-0000-4000-8000-000000000007"
        tests = ["tests/test_app.py::test_total[a - b]", "tests.test_calc.T.test_sum"]
        jobs = {new: dict(base, token=new, repo="example/app", commit="c" * 40, failed_tests=tests),
                old: dict(base, token=old),
                unfinished: dict(base, token=unfinished, repo=None, commit=None, failed_tests=None),
                blank: dict(base, token=blank, repo="", commit="", failed_tests=[])}
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / "store"
            store.mkdir(mode=0o700)
            result = collect_sources.vm02_jobs(store, settled_jobs(jobs))
            self.assertEqual((result["imported"], result["rejected"]), (4, 0))
            rows = {r["source_ref"]: r for r in coldfix.load_store(store)[1]["failures"].values()}
            current = rows["vm02-job:" + coldfix.digest(new)]
            self.assertEqual(current["repo"], "example/app")
            details = json.loads(current["error"])
            self.assertEqual((details["commit"], details["failed_tests"]), ("c" * 40, tests))
            for identifier in (old, unfinished, blank):
                legacy = rows["vm02-job:" + coldfix.digest(identifier)]
                self.assertEqual(legacy["repo"], "unknown")
                self.assertEqual(json.loads(legacy["error"]),
                                 dict(type="vm02-job", verdict="FAIL", exit=1, log_tail="guest failed"))

    def test_vm02_receipt_imported_before_identity_is_deduped_not_rejected(self):
        identifier = "00000000-0000-4000-8000-000000000006"
        legacy = dict(version="vm02-lifecycle-v2", verdict="FAIL", cleanup="gone", cleaned_at=1793865073,
                      started=1793865033, owner="coldfix", command=["pytest"], guest_exit=1, token=identifier)
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / "store"
            store.mkdir(mode=0o700)
            self.assertEqual(collect_sources.vm02_jobs(store, settled_jobs({identifier: legacy}))["imported"], 1)
            current = dict(legacy, repo="example/app", commit="c" * 40, failed_tests=["tests.test_a.T.test_b"])
            result = collect_sources.vm02_jobs(store, settled_jobs({identifier: current}))
            self.assertEqual((result["imported"], result["deduped"], result["rejected"]), (0, 1, 0))
            changed = collect_sources.vm02_jobs(store, settled_jobs({identifier: dict(current, guest_exit=2)}))
            self.assertEqual((changed["imported"], changed["rejected"]), (0, 1))

    def test_vm02_known_job_is_deduped_without_reloading_the_store(self):
        base = dict(version="vm02-lifecycle-v2", verdict="FAIL", cleanup="gone", cleaned_at=1793865073,
                    started=1793865033, owner="coldfix", command=["pytest"], guest_exit=1)
        known, fresh = "00000000-0000-4000-8000-000000000008", "00000000-0000-4000-8000-000000000009"
        job = dict(base, token=known, repo="example/app")
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / "store"
            store.mkdir(mode=0o700)
            self.assertEqual(collect_sources.vm02_jobs(store, settled_jobs({known: job}))["imported"], 1)
            before = coldfix.load_store(store)[1]["failures"]
            loads, imports = [], []
            real_load, real_import = coldfix.load_store, collect_sources.fi.import_records

            def load(root):
                loads.append(root)
                return real_load(root)

            def import_records(root, records):
                imports.append([row["source_ref"] for row in records])
                return real_import(root, records)

            with patch.object(coldfix, "load_store", load), patch.object(collect_sources.fi, "import_records", import_records):
                same = collect_sources.vm02_jobs(store, settled_jobs({known: job}))
                self.assertEqual((same["imported"], same["deduped"], same["rejected"]), (0, 1, 0))
                self.assertEqual((len(loads), imports), (1, []))
                for field, value in (("guest_exit", 2), ("owner", "other"), ("command", ["tox"]), ("started", 1793865034)):
                    with self.subTest(field=field):
                        imports.clear()
                        changed = collect_sources.vm02_jobs(store, settled_jobs({known: dict(job, **{field: value})}))
                        self.assertEqual((changed["imported"], changed["rejected"]), (0, 1))
                        self.assertEqual(imports, [["vm02-job:" + coldfix.digest(known)]])
                imports.clear()
                new = collect_sources.vm02_jobs(store, settled_jobs({known: job, fresh: dict(base, token=fresh)}))
                self.assertEqual((new["imported"], new["deduped"], new["rejected"]), (1, 1, 0))
                self.assertEqual(imports, [["vm02-job:" + coldfix.digest(fresh)]])
            after = coldfix.load_store(store)[1]["failures"]
            self.assertEqual({key: after[key] for key in before}, before)
            self.assertEqual(len(after), len(before) + 1)

    def test_vm02_receipt_with_malformed_identity_is_rejected(self):
        base = dict(version="vm02-lifecycle-v2", verdict="FAIL", cleanup="gone", cleaned_at=1793865073,
                    started=1793865033, owner="coldfix", command=["pytest"], guest_exit=1)
        malformed = [("repo", "no-slash"), ("repo", 7), ("commit", "abc"), ("commit", "C" * 40),
                     ("failed_tests", "tests.test_a"), ("failed_tests", ["line\nbreak"]),
                     ("failed_tests", [""]), ("failed_tests", [3])]
        for (field, value), verdict in itertools.product(malformed, ("FAIL", "PASS")):
            with self.subTest(field=field, value=value, verdict=verdict), tempfile.TemporaryDirectory() as folder:
                store = Path(folder) / "store"
                store.mkdir(mode=0o700)
                identifier = "00000000-0000-4000-8000-000000000005"
                job = dict(base, token=identifier, verdict=verdict, **{field: value})
                result = collect_sources.vm02_jobs(store, settled_jobs({identifier: job}))
                self.assertEqual((result["imported"], result["passed"], result["rejected"]), (0, 0, 1))

    def test_failure_json_import_and_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            store = work / 'store'
            store.mkdir(mode=0o700)
            fixture = Path(__file__).parent / 'fixtures' / 'failed-ci-run.json'
            config = work / 'sources.json'
            config.write_text(json.dumps({'vm': {'format': 'failure-json', 'command': ['cat', str(fixture)]}}))
            first = collect_sources.collect(store, config)['vm']
            second = collect_sources.collect(store, config)['vm']
            self.assertEqual((first['imported'], first['unavailable']), (1, 0))
            self.assertEqual((second['imported'], second['deduped']), (0, 1))
            self.assertEqual(len(coldfix.load_store(store)[1]['failures']), 1)


if __name__ == '__main__':
    unittest.main()
