#!/usr/local/bin/python3
"""OPNsense WAN observation, durable notification delivery, and bounded recovery."""
import argparse
import contextlib
import datetime
import fcntl
import ipaddress
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import syslog
import tempfile
import time
import urllib.parse

CONFIG = '/usr/local/etc/wan-watchdog.json'
STATE = '/var/db/opnsense-wan-watchdog'
PLUGINCTL = '/usr/local/sbin/pluginctl'
CONFIGCTL = '/usr/local/sbin/configctl'
CURL = '/usr/local/bin/curl'
PYTHON = '/usr/local/bin/python3'
HELPER = '/usr/local/libexec/wan_watchdog.py'
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
INTERFACE = re.compile(r'[a-z][a-z0-9]{0,31}\Z')
STATUS = re.compile(r'[a-z][a-z0-9_ -]{0,63}\Z')
DEFAULTS = dict(device='', sound='', priority=0, title='OPNsense Gateway Alert',
                boot_delay=90, recovery_enabled=False, recovery_interval=70,
                max_recovery_attempts=60, source_ipv4='',
                queue_limit=10000)


def log(event, **fields):
    syslog.syslog(syslog.LOG_NOTICE, json.dumps(dict(event=event, **fields), sort_keys=True))


def protected(path, directory=False):
    st = os.lstat(path)
    wanted = stat.S_ISDIR if directory else stat.S_ISREG
    if not wanted(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise ValueError('unsafe ownership, type, or permissions: ' + str(path))
    if not directory and st.st_nlink != 1:
        raise ValueError('hard-linked file rejected: ' + str(path))


def load_config(path=CONFIG):
    protected(path)
    with open(path, encoding='utf-8') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('configuration too large')
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('duplicate configuration key')
            out[key] = value
        return out
    data = json.loads(raw, object_pairs_hook=unique)
    allowed = set(DEFAULTS) | {'app_token', 'user_key', 'gateways'}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError('unknown configuration keys or invalid object')
    cfg = dict(DEFAULTS, **data)
    for key in ('app_token', 'user_key'):
        if not isinstance(cfg.get(key), str) or not re.fullmatch(r'[A-Za-z0-9]{30}', cfg[key]):
            raise ValueError('configure a valid ' + key)
    gateways = cfg.get('gateways')
    if not isinstance(gateways, dict) or not gateways or len(gateways) > 64:
        raise ValueError('gateways must contain 1..64 expected gateway names')
    interfaces = []
    for name, interface in gateways.items():
        if not NAME.fullmatch(name):
            raise ValueError('invalid gateway name')
        if interface is not None:
            if not isinstance(interface, str) or not INTERFACE.fullmatch(interface):
                raise ValueError('invalid logical interface token')
            interfaces.append(interface)
    if len(set(interfaces)) != len(interfaces):
        raise ValueError('duplicate recovery interface mapping')
    for key, low, high in [('boot_delay', 0, 600), ('recovery_interval', 30, 3600),
                           ('max_recovery_attempts', 1, 100),
                           ('queue_limit', 100, 100000), ('priority', -2, 1)]:
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError('invalid ' + key)
    if type(cfg['recovery_enabled']) is not bool:
        raise ValueError('recovery_enabled must be boolean')
    for key, limit in [('device', 128), ('sound', 64), ('title', 128)]:
        value = cfg[key]
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('invalid ' + key)
    if not isinstance(cfg['source_ipv4'], str):
        raise ValueError('source_ipv4 must be a string')
    if cfg['source_ipv4']:
        ipaddress.IPv4Address(cfg['source_ipv4'])
    return cfg


def snapshot():
    result = subprocess.run([PLUGINCTL, '-r', 'return_gateways_status'],
                            capture_output=True, timeout=15, check=False)
    if result.returncode or len(result.stdout) > 1048576:
        raise ValueError('gateway query failed')
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or not isinstance(data.get('dpinger'), dict):
        raise ValueError('invalid gateway response')
    return data['dpinger']


def status_for(data, name):
    value = data.get(name)
    if not isinstance(value, dict):
        return 'monitor_unavailable'
    raw = value.get('status')
    if not isinstance(raw, str) or any(ord(c) < 32 or ord(c) == 127 for c in raw) or not STATUS.fullmatch(raw.strip().lower()):
        return 'unknown'
    return raw.strip().lower()


class Store:
    def __init__(self, directory=STATE):
        self.directory = str(directory)
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        protected(self.directory, directory=True)
        self.path = os.path.join(self.directory, 'state.sqlite3')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        protected(self.path)
        self.db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS observations(gateway TEXT PRIMARY KEY,status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,gateway TEXT NOT NULL,old TEXT,
            new TEXT NOT NULL,observed REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
            due REAL NOT NULL DEFAULT 0,blocked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS recovery(
            gateway TEXT PRIMARY KEY,attempts INTEGER NOT NULL DEFAULT 0,
            unknowns INTEGER NOT NULL DEFAULT 0,due REAL NOT NULL DEFAULT 0);
        """)

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    @contextlib.contextmanager
    def worker_lock(self, name):
        path = os.path.join(self.directory, name + '.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            protected(path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def close(self):
        self.db.close()


def observe(store, cfg, names, query=snapshot, now=time.time):
    queued = []
    with store.transaction():
        data = query()
        for name in names:
            if name not in cfg['gateways']:
                log('ignore', gateway=name, reason='not_configured')
                continue
            current = status_for(data, name)
            row = store.db.execute('SELECT status FROM observations WHERE gateway=?', (name,)).fetchone()
            old = row['status'] if row else None
            if old == current:
                continue
            if old is not None or current != 'none':
                count = store.db.execute('SELECT count(*) FROM events').fetchone()[0]
                if count >= cfg['queue_limit']:
                    raise ValueError('notification queue full; observation not advanced')
                cursor = store.db.execute(
                    'INSERT INTO events(gateway,old,new,observed) VALUES(?,?,?,?)',
                    (name, old, current, now()))
                queued.append(dict(gateway=name, event_id=cursor.lastrowid, old=old, new=current))
            store.db.execute('INSERT OR REPLACE INTO observations VALUES(?,?)', (name, current))
    for event in queued:
        log('queued', **event)


def send(cfg, event):
    def display(value):
        return 'up' if value == 'none' else (value or 'not previously observed')
    stamp = datetime.datetime.fromtimestamp(event['observed'], datetime.timezone.utc).isoformat()
    payload = dict(token=cfg['app_token'], user=cfg['user_key'], priority=str(cfg['priority']),
                   title=cfg['title'] + ': ' + event['gateway'],
                   message='Gateway {}: {} -> {}. Observed {}. Event {}.'.format(
                       event['gateway'], display(event['old']), display(event['new']), stamp, event['id']))
    if cfg['device']:
        payload['device'] = cfg['device']
    if cfg['sound']:
        payload['sound'] = cfg['sound']
    body = urllib.parse.urlencode(payload).encode('utf-8')
    with tempfile.TemporaryDirectory(prefix='wan-pushover-') as directory:
        response = os.path.join(directory, 'response')
        args = [CURL, '--disable', '--silent', '--show-error', '--proto', '=https',
                '--connect-timeout', '5', '--max-time', '15', '--max-filesize', '65536',
                '--noproxy', '*',
                '--output', response, '--write-out', '%{http_code}',
                '--data-binary', '@-', '--header', 'Content-Type: application/x-www-form-urlencoded']
        if cfg['source_ipv4']:
            args += ['--interface', cfg['source_ipv4']]
        args += ['https://api.pushover.net/1/messages.json']
        try:
            result = subprocess.run(args, input=body, capture_output=True, timeout=20, check=False)
        except subprocess.TimeoutExpired:
            return 'retry', dict(result='process_timeout')
        except OSError:
            return 'blocked', dict(result='curl_unavailable')
        if result.returncode:
            return 'retry', dict(result='curl_error', curl_exit=result.returncode)
        try:
            http = int(result.stdout.strip())
        except ValueError:
            return 'retry', dict(result='invalid_http_status')
        if http != 200:
            retryable = http in (408, 425, 429) or 500 <= http <= 599
            return ('retry' if retryable else 'blocked'), dict(result='http_error', http_code=http)
        try:
            with open(response, 'rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError('oversize response')
            data = json.loads(raw)
            if not isinstance(data, dict) or type(data.get('status')) is not int:
                raise ValueError('invalid response')
        except (OSError, ValueError):
            return 'retry', dict(result='invalid_api_response')
        if data['status'] == 1:
            return 'success', dict(result='success')
        return 'blocked', dict(result='api_rejected')


def deliver(store, cfg, sender=send, now=time.time):
    with store.worker_lock('delivery') as acquired:
        if not acquired:
            return
        # One bounded attempt per gateway per invocation; ordering is per gateway.
        events = store.db.execute("""
            SELECT e.* FROM events e WHERE e.id=(
              SELECT min(first.id) FROM events first WHERE first.gateway=e.gateway)
            AND e.blocked=0 AND e.due<=? ORDER BY e.id LIMIT 64
            """, (now(),)).fetchall()
        deadline = time.monotonic() + 45
        for event in events:
            if time.monotonic() >= deadline:
                break
            outcome, detail = sender(cfg, event)
            with store.transaction():
                if outcome == 'success':
                    store.db.execute('DELETE FROM events WHERE id=?', (event['id'],))
                else:
                    attempts = event['attempts'] + 1
                    delay = (10, 20, 30, 60)[min(attempts - 1, 3)]
                    store.db.execute('UPDATE events SET attempts=?,due=?,blocked=? WHERE id=?',
                                     (attempts, now() + delay, int(outcome == 'blocked'), event['id']))
            log('notify', gateway=event['gateway'], event_id=event['id'],
                attempt=event['attempts'] + 1, **detail)


def reconfigure(interface):
    result = subprocess.run([CONFIGCTL, 'interface', 'reconfigure', interface],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=30, check=False)
    return result.returncode == 0


def recover(store, cfg, names, query=snapshot, action=reconfigure, now=time.time):
    if not cfg['recovery_enabled']:
        return
    with store.worker_lock('recovery') as acquired:
        if not acquired:
            return
        data = query()
        for name in names:
            interface = cfg['gateways'].get(name)
            if not interface:
                log('recovery_ignore', gateway=name, reason='not_mapped')
                continue
            status = status_for(data, name)
            with store.transaction():
                if status in ('none', 'force_down'):
                    store.db.execute('DELETE FROM recovery WHERE gateway=?', (name,))
                    continue
                store.db.execute('INSERT OR IGNORE INTO recovery(gateway) VALUES(?)', (name,))
                row = store.db.execute('SELECT * FROM recovery WHERE gateway=?', (name,)).fetchone()
                if row['due'] > now() or row['attempts'] >= cfg['max_recovery_attempts']:
                    continue
                if status in ('unknown', 'monitor_unavailable'):
                    store.db.execute('UPDATE recovery SET unknowns=unknowns+1,due=? WHERE gateway=?',
                                     (now() + cfg['recovery_interval'], name))
                    log('recovery_wait', gateway=name, status=status,
                        consecutive_unknowns=row['unknowns'] + 1)
                    continue
                if status != 'down':
                    log('recovery_ignore', gateway=name, status=status, reason='non_down_status')
                    continue
                # Reserve the attempt before execution to bound repeats after a crash.
                store.db.execute('UPDATE recovery SET attempts=attempts+1,unknowns=0,due=? WHERE gateway=?',
                                 (now() + cfg['recovery_interval'], name))
            try:
                success = action(interface)
            except (OSError, subprocess.TimeoutExpired):
                success = False
            log('reconfigure', gateway=name, interface=interface, attempt=row['attempts'] + 1,
                success=success)


def spawn(command, gateways=None):
    args = [PYTHON, HELPER, command]
    if gateways:
        args.append(','.join(gateways))
    subprocess.Popen(args, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=True)


def main():
    os.umask(0o077)
    syslog.openlog('wan-watchdog')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['monitor', 'recover-event', 'boot', 'worker',
                                          'recover', 'tick', 'status', 'retry-blocked', 'check-config'])
    parser.add_argument('gateways', nargs='?', default='')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('must run as root')
    store = None
    try:
        cfg = load_config()
        if args.command == 'check-config':
            print('Configuration syntax, permissions, and values OK; interface existence not verified.')
            return 0
        names = list(cfg['gateways'])
        if args.command in ('monitor', 'recover-event') or (args.command == 'recover' and args.gateways):
            if len(args.gateways) > 4096:
                raise ValueError('gateway argument too long')
            names = list(dict.fromkeys(args.gateways.split(','))) if args.gateways else []
            if any(not NAME.fullmatch(name) for name in names):
                raise ValueError('invalid gateway argument')
        if args.command == 'boot':
            spawn('worker')
            time.sleep(cfg['boot_delay'])
        store = Store()
        if args.command == 'status':
            print(json.dumps(dict(pending=store.db.execute('SELECT count(*) FROM events').fetchone()[0],
                                  blocked=store.db.execute('SELECT count(*) FROM events WHERE blocked=1').fetchone()[0])))
        elif args.command == 'retry-blocked':
            with store.transaction():
                store.db.execute('UPDATE events SET blocked=0,due=0 WHERE blocked=1')
            log('blocked_events_released')
        elif args.command in ('monitor', 'boot'):
            observe(store, cfg, names)
            spawn('worker')
        elif args.command == 'recover-event':
            # Hooks never reset interfaces inline. Only configured event gateways are selected.
            selected = [name for name in names if name in cfg['gateways']]
            if cfg['recovery_enabled'] and selected:
                spawn('recover', selected)
        elif args.command == 'worker':
            deliver(store, cfg)
        elif args.command == 'recover':
            recover(store, cfg, names)
        elif args.command == 'tick':
            # Failure in observation must not prevent delivery of already queued events.
            failed = False
            try:
                observe(store, cfg, names)
            except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError):
                failed = True
                log('observation_failed')
            spawn('worker')
            spawn('recover')
            return int(failed)
        return 0
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        # Do not echo exception text: third-party responses/configuration may include secrets.
        log('error', command=args.command, error_type=type(error).__name__)
        print('wan-watchdog: operation failed; inspect protected configuration and system logs', file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
