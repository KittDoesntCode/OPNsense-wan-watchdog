"""Isolated contract and failure tests; never invoke real firewall commands."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('watchdog', ROOT / 'wan_watchdog.py')
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = dict(w.DEFAULTS, app_token='A'*30, user_key='B'*30,
                        gateways={'G': 'wan', 'H': 'opt1'})
        self.store = w.Store(self.root / 'state')
        self.clock = 1000
        self.now = lambda: self.clock
        self.logging = patch.object(w, 'log')
        self.logging.start()

    def tearDown(self):
        self.logging.stop()
        self.store.close()
        self.temp.cleanup()

    def observe(self, state, name='G'):
        w.observe(self.store, self.cfg, [name], lambda: {name: {'status': state}}, self.now)

    def events(self):
        return self.store.db.execute('SELECT * FROM events ORDER BY id').fetchall()

    def config(self, **updates):
        path = self.root / 'config.json'
        path.write_text(json.dumps(dict(self.cfg, **updates)))
        path.chmod(0o600)
        return path

    def test_baseline_and_duplicates(self):
        self.observe('none')
        self.observe('none')
        self.assertEqual(len(self.events()), 0)
        self.observe('down')
        self.observe('down')
        self.assertEqual(len(self.events()), 1)

    def test_first_problem(self):
        self.observe('down')
        self.assertIsNone(self.events()[0]['old'])

    def test_transition_order_and_failed_outage(self):
        for status in ('none', 'loss', 'down', 'none'):
            self.observe(status)
        self.assertEqual([e['new'] for e in self.events()], ['loss', 'down', 'none'])
        w.deliver(self.store, self.cfg, lambda *_: ('retry', {'result': 'error'}), self.now)
        sent = []
        def send(cfg, event):
            sent.append(event['new'])
            return 'success', {'result': 'success'}
        w.deliver(self.store, self.cfg, send, self.now)
        self.assertEqual(sent, [])
        self.clock += 10
        for _ in range(3):
            w.deliver(self.store, self.cfg, send, self.now)
        self.assertEqual(sent, ['loss', 'down', 'none'])

    def test_backoff(self):
        self.observe('down')
        for delay in (10, 20, 30, 60, 60):
            w.deliver(self.store, self.cfg, lambda *_: ('retry', {'result': 'error'}), self.now)
            self.assertEqual(self.events()[0]['due'], self.clock + delay)
            self.clock += delay

    def test_blocking_is_per_gateway(self):
        self.observe('down')
        self.observe('none')
        self.observe('down', 'H')
        def send(cfg, event):
            return ('blocked' if event['gateway'] == 'G' else 'success'), {'result': 'test'}
        w.deliver(self.store, self.cfg, send, self.now)
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(self.events()[0]['blocked'], 1)

    def test_persistence(self):
        self.observe('down')
        other = w.Store(self.root / 'state')
        try:
            self.assertEqual(other.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        finally:
            other.close()

    def test_query_failure_and_capacity_rollback(self):
        self.observe('down')
        self.cfg['queue_limit'] = 1
        with self.assertRaises(ValueError):
            self.observe('none')
        with self.assertRaises(ValueError):
            w.observe(self.store, self.cfg, ['G'], Mock(side_effect=ValueError('query')))
        row = self.store.db.execute('SELECT status FROM observations').fetchone()
        self.assertEqual(row[0], 'down')

    def test_missing_and_invalid_status(self):
        self.assertEqual(w.status_for({}, 'G'), 'monitor_unavailable')
        for raw in (None, {}, 0, '', '\nnone\n', 'bad;command'):
            with self.subTest(raw=raw):
                self.assertEqual(w.status_for({'G': {'status': raw}}, 'G'), 'unknown')

    def test_unknown_name_ignored(self):
        self.observe('down', 'UNCONFIGURED')
        self.assertEqual(len(self.events()), 0)

    def test_concurrent_observation(self):
        barrier = threading.Barrier(4)
        def task(_):
            store = w.Store(self.root / 'state')
            try:
                barrier.wait(timeout=5)
                w.observe(store, self.cfg, ['G'], lambda: {'G': {'status': 'down'}})
            finally:
                store.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(task, range(4)))
        self.assertEqual(len(self.events()), 1)

    def test_locks(self):
        for name in ('delivery', 'recovery'):
            with self.store.worker_lock(name) as first:
                with self.store.worker_lock(name) as second:
                    self.assertTrue(first)
                    self.assertFalse(second)

    def test_delivery_lock_and_deadline(self):
        self.observe('down')
        sender = Mock()
        with self.store.worker_lock('delivery'):
            w.deliver(self.store, self.cfg, sender)
        with patch.object(w.time, 'monotonic', side_effect=[0, 46]):
            w.deliver(self.store, self.cfg, sender)
        sender.assert_not_called()

    def test_interruption_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.db.execute("INSERT INTO observations VALUES('G','down')")
                raise RuntimeError('interruption')
        self.assertEqual(len(self.events()), 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM observations').fetchone()[0], 0)

    def test_read_only_failure(self):
        self.store.db.execute('PRAGMA query_only=ON')
        with self.assertRaises(sqlite3.OperationalError):
            self.observe('down')
        self.assertEqual(len(self.events()), 0)

    def test_config(self):
        self.assertEqual(w.load_config(self.config())['gateways'], self.cfg['gateways'])
        invalid = [dict(priority=2), dict(priority=True), dict(boot_delay=-1),
                   dict(gateways={'../bad': 'wan'}), dict(gateways={'G': '--bad'}),
                   dict(gateways={'G': 'wan', 'H': 'wan'}), dict(app_token='bad'),
                   dict(source_ipv4=';reboot'), dict(title='bad\nline'), dict(typo=1)]
        for update in invalid:
            with self.subTest(update=update), self.assertRaises(ValueError):
                w.load_config(self.config(**update))

    def test_config_duplicate_and_oversize(self):
        path = self.config()
        for text in ('{"gateways":{},"gateways":{}}', ' '*65537):
            path.write_text(text)
            with self.assertRaises(ValueError):
                w.load_config(path)

    def test_permissions_and_links(self):
        path = self.config()
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            w.load_config(path)
        path.chmod(0o600)
        link = self.root / 'link'
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            w.load_config(link)
        os.link(path, self.root / 'hardlink')
        with self.assertRaises(ValueError):
            w.load_config(path)
        self.assertEqual((self.root / 'state').stat().st_mode & 0o777, 0o700)
        self.assertEqual(Path(self.store.path).stat().st_mode & 0o777, 0o600)

    def test_state_symlink(self):
        link = self.root / 'state-link'
        link.symlink_to(self.root / 'state')
        with self.assertRaises(ValueError):
            w.Store(link)

    def test_snapshot_failures(self):
        for code, body in ((1, b''), (0, b'bad'), (0, b'{}')):
            result = subprocess.CompletedProcess([], code, body, b'')
            with patch.object(w.subprocess, 'run', return_value=result), self.assertRaises(ValueError):
                w.snapshot()

    def test_recovery_disabled(self):
        action = Mock()
        w.recover(self.store, self.cfg, ['G'], action=action)
        action.assert_not_called()

    def test_recovery_limit_spacing_and_reset(self):
        self.cfg.update(recovery_enabled=True, max_recovery_attempts=2)
        action = Mock(return_value=True)
        query = lambda: {'G': {'status': 'down'}}
        for delta in (0, 10, 60, 70):
            self.clock += delta
            w.recover(self.store, self.cfg, ['G'], query, action, self.now)
        self.assertEqual(action.call_count, 2)
        action.assert_called_with('wan')
        for state in ('none', 'force_down'):
            w.recover(self.store, self.cfg, ['G'], lambda: {'G': {'status': state}}, action, self.now)
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM recovery').fetchone()[0], 0)

    def test_recovery_unknown_loss_and_unmapped(self):
        self.cfg['recovery_enabled'] = True
        action = Mock()
        for query in (lambda: {}, lambda: {'G': {'status': 'loss'}}):
            w.recover(self.store, self.cfg, ['G'], query, action, self.now)
            self.clock += 70
        self.cfg['gateways']['G'] = None
        w.recover(self.store, self.cfg, ['G'], lambda: {'G': {'status': 'down'}}, action, self.now)
        action.assert_not_called()

    def test_recovery_failure_counted(self):
        self.cfg['recovery_enabled'] = True
        w.recover(self.store, self.cfg, ['G'], lambda: {'G': {'status': 'down'}},
                  Mock(side_effect=subprocess.TimeoutExpired('test', 30)), self.now)
        self.assertEqual(self.store.db.execute('SELECT attempts FROM recovery').fetchone()[0], 1)

    def test_send_classifications(self):
        self.observe('down')
        cases = [(28, b'000', b'', 'retry'), (0, b'503', b'', 'retry'),
                 (0, b'429', b'', 'retry'), (0, b'400', b'', 'blocked'),
                 (0, b'200', b'{"status":1}', 'success'),
                 (0, b'200', b'{"status":0}', 'blocked'),
                 (0, b'200', b'{"status":true}', 'retry'),
                 (0, b'200', b'bad', 'retry'), (0, b'garbage', b'', 'retry')]
        for code, http, body, expected in cases:
            def run(args, **kwargs):
                self.assertNotIn(self.cfg['app_token'], ' '.join(args))
                self.assertIn(b'token=', kwargs['input'])
                Path(args[args.index('--output')+1]).write_bytes(body)
                return subprocess.CompletedProcess(args, code, http, b'private')
            with self.subTest(expected=expected, http=http), patch.object(w.subprocess, 'run', side_effect=run):
                self.assertEqual(w.send(self.cfg, self.events()[0])[0], expected)

    def test_send_process_errors(self):
        self.observe('down')
        for error, expected in ((OSError('missing'), 'blocked'), (subprocess.TimeoutExpired('curl', 20), 'retry')):
            with patch.object(w.subprocess, 'run', side_effect=error):
                self.assertEqual(w.send(self.cfg, self.events()[0])[0], expected)

    def test_real_subprocess_contract(self):
        fake = self.root / 'curl'
        fake.write_text('#!/usr/bin/env python3\nimport sys,pathlib\na=sys.argv\nassert b"token=" in sys.stdin.buffer.read()\nassert a[1]=="--disable"\npathlib.Path(a[a.index("--output")+1]).write_text(\'{"status":1}\')\nprint("200",end="")\n')
        fake.chmod(0o700)
        self.observe('down')
        with patch.object(w, 'CURL', str(fake)):
            self.assertEqual(w.send(self.cfg, self.events()[0])[0], 'success')

    def test_detached_spawn(self):
        with patch.object(w.subprocess, 'Popen') as spawned:
            w.spawn('recover', ['G'])
        args, kwargs = spawned.call_args
        self.assertEqual(args[0], [w.PYTHON, w.HELPER, 'recover', 'G'])
        self.assertTrue(kwargs['start_new_session'])
        self.assertNotIn('shell', kwargs)

    def invoke(self, command, argument=''):
        with patch.object(w.sys, 'argv', ['helper', command, argument]), \
             patch.object(w.os, 'geteuid', return_value=0), \
             patch.object(w, 'load_config', return_value=self.cfg), \
             patch.object(w, 'Store', return_value=self.store):
            return w.main()

    def test_cli_injection_rejected(self):
        with patch.object(w.sys, 'stderr'):
            self.assertEqual(self.invoke('monitor', '../../bad'), 1)

    def test_tick_failure_still_starts_workers(self):
        with patch.object(w, 'observe', side_effect=ValueError('query')), patch.object(w, 'spawn') as spawned:
            self.assertEqual(self.invoke('tick'), 1)
            self.assertEqual(spawned.call_args_list, [call('worker'), call('recover')])

    def test_boot_delay(self):
        with patch.object(w.time, 'sleep') as sleep, patch.object(w, 'spawn'), patch.object(w, 'observe'):
            self.assertEqual(self.invoke('boot'), 0)
            sleep.assert_called_once_with(90)

    def test_wrappers(self):
        for name, command in [('50-wan-watchdog', 'recover-event'), ('60-wan-pushover', 'monitor'),
                              ('20-wan-pushover-bootcheck', 'boot')]:
            path = ROOT / name
            subprocess.run(['/bin/sh', '-n', str(path)], check=True)
            self.assertIn('wan_watchdog.py ' + command, path.read_text())


if __name__ == '__main__':
    unittest.main()
