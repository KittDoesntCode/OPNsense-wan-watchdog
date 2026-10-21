"""Offline tests: real SQLite/locks; no live router or Pushover calls."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('w', ROOT / 'wan_watchdog.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = w.database(self.root / 'state')
        self.cfg = dict(w.DEFAULTS, app_token='A'*30, user_key='B'*30, gateways={'G': 'wan', 'H': 'opt1'})
        self.clock = 1000
        self.logs = patch.object(w, 'log')
        self.logs.start()

    def tearDown(self):
        self.logs.stop()
        self.db.close()
        self.temp.cleanup()

    def observe(self, state, name='G'):
        return w.observe(self.db, [name], lambda: {name: {'status': state}})

    def events(self):
        return self.db.execute('SELECT * FROM events ORDER BY id').fetchall()

    def deliver(self, sender):
        def sleep(seconds):
            self.clock += seconds
        w.deliver(self.db, self.cfg, self.root / 'state', sender, sleep, lambda: self.clock)

    def config(self, **updates):
        path = self.root / 'config'
        path.write_text(json.dumps(dict(self.cfg, **updates)))
        path.chmod(0o600)
        return path

    def test_healthy_baseline_and_duplicates(self):
        self.assertFalse(self.observe('none'))
        self.assertFalse(self.observe('none'))
        self.observe('down')
        self.observe('down')
        self.assertEqual(len(self.events()), 1)

    def test_first_problem(self):
        self.observe('down')
        self.assertIsNone(self.events()[0]['old'])

    def test_failed_outage_preserves_recovery_and_order(self):
        for state in ('none', 'loss', 'down', 'none'):
            self.observe(state)
        sent = []
        def sender(cfg, event):
            sent.append(event['new'])
            return ('retry', 'timeout') if len(sent) == 1 else ('success', 'accepted')
        self.deliver(sender)
        self.assertEqual(sent, ['loss', 'loss', 'down', 'none'])
        self.assertEqual(len(self.events()), 0)
        self.assertEqual(self.clock, 1010)

    def test_backoff_and_blocking(self):
        self.observe('down')
        calls = []
        def sender(cfg, event):
            calls.append(self.clock)
            return ('retry', 'timeout') if len(calls) < 6 else ('blocked', 'api_rejected')
        self.deliver(sender)
        self.assertEqual(calls, [1000, 1010, 1030, 1060, 1120, 1180])
        self.assertEqual(self.events()[0]['blocked'], 1)

    def test_blocked_gateway_does_not_block_others(self):
        self.observe('down')
        self.observe('none')
        self.observe('down', 'H')
        sent = []
        def sender(cfg, event):
            sent.append(event['gateway'])
            return ('blocked' if event['gateway'] == 'G' else 'success'), 'test'
        self.deliver(sender)
        self.assertEqual(sent, ['G', 'H'])
        self.assertEqual(len(self.events()), 2)

    def test_empty_worker_does_not_sleep_or_send(self):
        sender, sleep = Mock(), Mock()
        w.deliver(self.db, self.cfg, self.root / 'state', sender, sleep)
        sender.assert_not_called()
        sleep.assert_not_called()

    def test_exit_releases_lock_before_queue_commit(self):
        # At COMMIT after the empty check, another process must be able to obtain delivery lock.
        checked = []
        def trace(sql):
            if sql == 'COMMIT':
                with w.lock(self.root / 'state', 'delivery', False) as acquired:
                    checked.append(acquired is not None)
        self.db.set_trace_callback(trace)
        self.deliver(Mock())
        self.db.set_trace_callback(None)
        self.assertEqual(checked, [True])

    def test_concurrent_observers(self):
        def task(_):
            db = w.database(self.root / 'state')
            try:
                w.observe(db, ['G'], lambda: {'G': {'status': 'down'}})
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(task, range(8)))
        self.assertEqual(len(self.events()), 1)

    def test_persistence_and_query_failure(self):
        self.observe('down')
        with self.assertRaises(ValueError):
            w.observe(self.db, ['G'], Mock(side_effect=ValueError('query')))
        other = w.database(self.root / 'state')
        try:
            self.assertEqual(other.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        finally:
            other.close()

    def test_lock_exclusion(self):
        with w.lock(self.root, 'test') as first:
            with w.lock(self.root, 'test', False) as second:
                self.assertIsNotNone(first)
                self.assertIsNone(second)

    def test_permissions_links_and_unsafe_values(self):
        path = self.config()
        self.assertEqual(w.configuration(path)['gateways'], self.cfg['gateways'])
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            w.configuration(path)
        path.chmod(0o600)
        link = self.root / 'link'
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            w.configuration(link)
        os.link(path, self.root / 'hardlink')
        with self.assertRaises(ValueError):
            w.configuration(path)
        (self.root / 'hardlink').unlink()
        for update in [dict(priority=True), dict(priority=2), dict(app_token='bad'),
                       dict(gateways={'../bad': 'wan'}), dict(gateways={'G': '--bad'}),
                       dict(gateways={'G': 'wan', 'H': 'wan'}), dict(typo=True), dict(title='bad\n')]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                w.configuration(self.config(**update))

    def test_invalid_json_and_status(self):
        path = self.config()
        for text in ('{"gateways":{},"gateways":{}}', ' '*65537):
            path.write_text(text)
            with self.assertRaises(ValueError):
                w.configuration(path)
        self.assertEqual(w.status({}, 'G'), 'monitor_unavailable')
        for raw in ('', '\nnone', None, {}, 'bad;command'):
            self.assertEqual(w.status({'G': {'status': raw}}, 'G'), 'unknown')

    def test_sender_fault_classification_and_secret_handling(self):
        self.observe('down')
        for code, http, body, outcome in [(28, b'000', b'', 'retry'), (0, b'503', b'', 'retry'),
                (0, b'429', b'', 'retry'), (0, b'400', b'', 'blocked'),
                (0, b'200', b'{"status":1}', 'success'), (0, b'200', b'{"status":0}', 'blocked'),
                (0, b'200', b'bad', 'retry'), (0, b'200', b'{"status":true}', 'retry')]:
            def run(args, **kwargs):
                self.assertNotIn(self.cfg['app_token'], ' '.join(args))
                self.assertIn(b'token=', kwargs['input'])
                Path(args[args.index('--output')+1]).write_bytes(body)
                return subprocess.CompletedProcess(args, code, http, b'private')
            with self.subTest(outcome=outcome), patch.object(w.subprocess, 'run', side_effect=run):
                self.assertEqual(w.send(self.cfg, self.events()[0])[0], outcome)

    def test_sender_process_errors(self):
        self.observe('down')
        for error, expected in [(OSError(), 'blocked'), (subprocess.TimeoutExpired('curl', 20), 'retry')]:
            with patch.object(w.subprocess, 'run', side_effect=error):
                self.assertEqual(w.send(self.cfg, self.events()[0])[0], expected)

    def test_recovery_only_while_down(self):
        self.cfg['recovery_enabled'] = True
        query = Mock(side_effect=[{'G': {'status': 'down'}}, {'G': {'status': 'none'}}])
        action, sleep = Mock(return_value=True), Mock()
        w.recover(self.cfg, 'G', self.root, query, action, sleep)
        action.assert_called_once_with('wan')
        sleep.assert_called_once_with(70)

    def test_recovery_unknown_grace(self):
        self.cfg['recovery_enabled'] = True
        query = Mock(side_effect=[{}, {}, {'G': {'status': 'down'}}, {'G': {'status': 'none'}}])
        action, sleep = Mock(return_value=True), Mock()
        w.recover(self.cfg, 'G', self.root, query, action, sleep)
        action.assert_called_once_with('wan')
        self.assertEqual(sleep.call_count, 3)

    def test_recovery_unknown_never_resets(self):
        self.cfg['recovery_enabled'] = True
        action, sleep = Mock(), Mock()
        w.recover(self.cfg, 'G', self.root, lambda: {}, action, sleep)
        action.assert_not_called()
        self.assertEqual(sleep.call_count, 3)

    def test_recovery_overrides_and_limit(self):
        action = Mock(return_value=True)
        w.recover(self.cfg, 'G', self.root, action=action)
        action.assert_not_called()
        self.cfg.update(recovery_enabled=True, max_recovery_attempts=2)
        for state in ('force_down', 'loss'):
            w.recover(self.cfg, 'G', self.root, lambda: {'G': {'status': state}}, action, Mock())
        action.assert_not_called()
        w.recover(self.cfg, 'G', self.root, lambda: {'G': {'status': 'down'}}, action, Mock())
        self.assertEqual(action.call_count, 2)

    def test_spawn_detaches(self):
        with patch.object(w.subprocess, 'Popen') as spawned:
            w.spawn('recover', 'G')
        args, kwargs = spawned.call_args
        self.assertEqual(args[0], [w.PYTHON, w.HELPER, 'recover', 'G'])
        self.assertTrue(kwargs['start_new_session'])
        self.assertNotIn('shell', kwargs)

    def invoke(self, command, argument=''):
        with patch.object(w.sys, 'argv', ['helper', command, argument]), \
             patch.object(w.os, 'geteuid', return_value=0), \
             patch.object(w, 'configuration', return_value=self.cfg), \
             patch.object(w, 'database', return_value=self.db):
            return w.main()

    def test_cli_invalid_argument(self):
        with patch.object(w.sys, 'stderr'):
            self.assertEqual(self.invoke('monitor', '../../bad'), 1)

    def test_healthy_hook_does_not_spawn_worker(self):
        with patch.object(w, 'observe', return_value=False), patch.object(w, 'spawn') as spawned:
            self.assertEqual(self.invoke('monitor', 'G'), 0)
            spawned.assert_not_called()

    def test_disabled_recovery_does_not_query_or_open_database(self):
        with patch.object(w, 'snapshot') as query:
            self.assertEqual(self.invoke('recover-event', 'G'), 0)
            query.assert_not_called()

    def test_boot_delay(self):
        with patch.object(w, 'observe', return_value=False), patch.object(w.time, 'sleep') as sleep:
            self.assertEqual(self.invoke('boot'), 0)
            sleep.assert_called_once_with(90)


    def test_queue_capacity_rolls_back(self):
        with self.db:
            self.db.executemany('INSERT INTO events(gateway,new,observed) VALUES(?,?,?)',
                                [('G', 'down', 1)] * 10000)
        with self.assertRaises(ValueError):
            self.observe('down', 'H')
        self.assertIsNone(self.db.execute("SELECT 1 FROM observations WHERE gateway='H'").fetchone())

    def test_real_transport_subprocess(self):
        self.observe('down')
        fake = self.root / 'curl'
        fake.write_text('#!/usr/bin/env python3\nimport sys,pathlib\na=sys.argv\nassert b"token=" in sys.stdin.buffer.read()\npathlib.Path(a[a.index("--output")+1]).write_text(\'{"status":1}\')\nprint("200",end="")\n')
        fake.chmod(0o700)
        with patch.object(w, 'CURL', str(fake)):
            self.assertEqual(w.send(self.cfg, self.events()[0])[0], 'success')

    def test_snapshot_rejects_failure(self):
        for code, body in [(1, b''), (0, b'broken'), (0, b'{}')]:
            with patch.object(w.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, body, b'')):
                with self.assertRaises(ValueError):
                    w.snapshot()



if __name__ == '__main__':
    unittest.main()
