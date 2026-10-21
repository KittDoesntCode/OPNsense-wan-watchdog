#!/usr/local/bin/python3
"""Event-driven OPNsense alerts. No cron, healthy-state polling, or resident daemon."""
import argparse
from contextlib import contextmanager
import fcntl
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
from urllib.parse import urlencode

CONFIG = '/usr/local/etc/wan-watchdog.json'
STATE = '/var/db/opnsense-wan-watchdog'
RUNTIME = '/var/run/opnsense-wan-watchdog'
HELPER = '/usr/local/libexec/wan_watchdog.py'
PYTHON = '/usr/local/bin/python3'
PLUGINCTL = '/usr/local/sbin/pluginctl'
CONFIGCTL = '/usr/local/sbin/configctl'
CURL = '/usr/local/bin/curl'
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
STATUS = re.compile(r'[a-z][a-z0-9_ -]{0,63}\Z')
DEFAULTS = dict(boot_delay=90, recovery_enabled=False, recovery_interval=70,
                max_recovery_attempts=60, title='OPNsense Gateway Alert',
                device='', sound='', priority=0)


def log(event, **fields):
    syslog.syslog(syslog.LOG_NOTICE, json.dumps(dict(event=event, **fields)))


def private(path, directory=False):
    st = os.lstat(path)
    correct_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not correct_type(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise ValueError('unsafe file ownership, permissions or type')
    if not directory and st.st_nlink != 1:
        raise ValueError('hard link rejected')


def configuration(path=CONFIG):
    private(path)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate configuration key')
            result[key] = value
        return result
    with open(path, encoding='utf-8') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('oversize configuration')
    data = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(data, dict) or set(data) - (set(DEFAULTS) | {'app_token', 'user_key', 'gateways'}):
        raise ValueError('unknown configuration fields')
    cfg = dict(DEFAULTS, **data)
    for key in ('app_token', 'user_key'):
        if not isinstance(cfg.get(key), str) or not re.fullmatch(r'[A-Za-z0-9]{30}', cfg[key]):
            raise ValueError('invalid credentials')
    gateways = cfg.get('gateways')
    if not isinstance(gateways, dict) or not 1 <= len(gateways) <= 64:
        raise ValueError('invalid gateways')
    interfaces = []
    for name, interface in gateways.items():
        if not NAME.fullmatch(name):
            raise ValueError('invalid gateway name')
        if interface is not None:
            if not isinstance(interface, str) or not re.fullmatch(r'[a-z][a-z0-9]{0,31}', interface):
                raise ValueError('invalid logical interface')
            interfaces.append(interface)
    if len(set(interfaces)) != len(interfaces):
        raise ValueError('duplicate recovery interface')
    for key, low, high in [('boot_delay', 0, 600), ('recovery_interval', 30, 3600),
                           ('max_recovery_attempts', 1, 100), ('priority', -2, 1)]:
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError('invalid numeric setting')
    if type(cfg['recovery_enabled']) is not bool:
        raise ValueError('invalid recovery_enabled')
    for key in ('title', 'device', 'sound'):
        value = cfg[key]
        if not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('invalid text setting')
    return cfg


@contextmanager
def lock(directory, name, blocking=True):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    private(directory, directory=True)
    path = os.path.join(directory, name + '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield None
        else:
            # Early release while holding the queue transaction closes the empty-worker race.
            yield lambda: fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def database(directory=STATE):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    private(directory, directory=True)
    path = os.path.join(directory, 'state.sqlite3')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    private(path)
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA synchronous=FULL')
    db.executescript("""
    CREATE TABLE IF NOT EXISTS observations(gateway TEXT PRIMARY KEY,status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,gateway TEXT NOT NULL,old TEXT,new TEXT NOT NULL,
      observed REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
      due REAL NOT NULL DEFAULT 0,blocked INTEGER NOT NULL DEFAULT 0);
    """)
    return db


def snapshot():
    result = subprocess.run([PLUGINCTL, '-r', 'return_gateways_status'],
                            capture_output=True, timeout=15, check=False)
    if result.returncode or len(result.stdout) > 1048576:
        raise ValueError('gateway query failed')
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or not isinstance(data.get('dpinger'), dict):
        raise ValueError('invalid gateway response')
    return data['dpinger']


def status(data, name):
    entry = data.get(name)
    if not isinstance(entry, dict):
        return 'monitor_unavailable'
    raw = entry.get('status')
    if not isinstance(raw, str) or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return 'unknown'
    raw = raw.strip().lower()
    return raw if STATUS.fullmatch(raw) else 'unknown'


def observe(db, names, query=snapshot):
    # Query under the write transaction: concurrent observers cannot apply stale snapshots.
    with db:
        db.execute('BEGIN IMMEDIATE')
        data = query()
        for name in names:
            current = status(data, name)
            row = db.execute('SELECT status FROM observations WHERE gateway=?', (name,)).fetchone()
            old = row[0] if row else None
            if current == old:
                continue
            if old is not None or current != 'none':
                if db.execute('SELECT count(*) FROM events').fetchone()[0] >= 10000:
                    raise ValueError('queue full; observation not advanced')
                db.execute('INSERT INTO events(gateway,old,new,observed) VALUES(?,?,?,?)',
                           (name, old, current, time.time()))
            db.execute('INSERT OR REPLACE INTO observations VALUES(?,?)', (name, current))
    return bool(db.execute('SELECT 1 FROM events WHERE blocked=0 LIMIT 1').fetchone())


def send(cfg, event):
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(event['observed']))
    old = 'up' if event['old'] == 'none' else (event['old'] or 'first observation')
    new = 'up' if event['new'] == 'none' else event['new']
    body = dict(token=cfg['app_token'], user=cfg['user_key'], priority=cfg['priority'],
                title=cfg['title'] + ': ' + event['gateway'],
                message='{}: {} -> {} at {} (event {}).'.format(event['gateway'], old, new, stamp, event['id']))
    for key in ('device', 'sound'):
        if cfg[key]:
            body[key] = cfg[key]
    with tempfile.TemporaryDirectory(prefix='wan-alert-') as directory:
        response = os.path.join(directory, 'response')
        args = [CURL, '--disable', '--silent', '--show-error', '--proto', '=https',
                '--noproxy', '*', '--connect-timeout', '5', '--max-time', '15',
                '--max-filesize', '65536', '--output', response, '--write-out', '%{http_code}',
                '--header', 'Content-Type: application/x-www-form-urlencoded', '--data-binary', '@-',
                'https://api.pushover.net/1/messages.json']
        try:
            result = subprocess.run(args, input=urlencode(body).encode(), capture_output=True,
                                    timeout=20, check=False)
        except subprocess.TimeoutExpired:
            return 'retry', 'process_timeout'
        except OSError:
            return 'blocked', 'curl_unavailable'
        if result.returncode:
            return 'retry', 'curl_exit_' + str(result.returncode)
        try:
            http = int(result.stdout.strip())
        except ValueError:
            return 'retry', 'invalid_http_status'
        if http != 200:
            retry = http in (408, 425, 429) or 500 <= http <= 599
            return ('retry' if retry else 'blocked'), 'http_' + str(http)
        try:
            with open(response, 'rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError('oversize response')
            reply = json.loads(raw)
            if not isinstance(reply, dict) or type(reply.get('status')) is not int:
                raise ValueError('invalid API response')
        except (ValueError, OSError):
            return 'retry', 'invalid_api_response'
        return ('success', 'accepted') if reply['status'] == 1 else ('blocked', 'api_rejected')


def deliver(db, cfg, directory=STATE, sender=send, sleep=time.sleep, now=time.time):
    with lock(directory, 'delivery', blocking=False) as release:
        if release is None:
            return
        while True:
            with db:
                db.execute('BEGIN IMMEDIATE')
                event = db.execute("""
                SELECT e.* FROM events e WHERE e.blocked=0 AND e.id=(
                  SELECT min(first.id) FROM events first WHERE first.gateway=e.gateway)
                ORDER BY e.due,e.id LIMIT 1
                """).fetchone()
                if event is None:
                    release()
                    return
            if event['due'] > now():
                sleep(min(event['due'] - now(), 60))
                continue
            outcome, detail = sender(cfg, event)
            with db:
                if outcome == 'success':
                    db.execute('DELETE FROM events WHERE id=?', (event['id'],))
                else:
                    attempt = event['attempts'] + 1
                    delay = (10, 20, 30, 60)[min(attempt - 1, 3)]
                    db.execute('UPDATE events SET attempts=?,due=?,blocked=? WHERE id=?',
                               (attempt, now() + delay, int(outcome == 'blocked'), event['id']))
            log('notify', gateway=event['gateway'], event_id=event['id'],
                attempt=event['attempts'] + 1, result=detail)


def reconfigure(interface):
    result = subprocess.run([CONFIGCTL, 'interface', 'reconfigure', interface],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
    return result.returncode == 0


def recover(cfg, name, directory=RUNTIME, query=snapshot, action=reconfigure, sleep=time.sleep):
    interface = cfg['gateways'].get(name)
    if not cfg['recovery_enabled'] or not interface:
        return
    with lock(directory, 'recovery-' + interface, blocking=False) as acquired:
        if acquired is None:
            return
        unknowns = 0
        for attempt in range(1, cfg['max_recovery_attempts'] + 1):
            # Allow a bounded grace period for monitor restarts without interpreting it as recovery.
            current = status(query(), name)
            while current in ('unknown', 'monitor_unavailable') and unknowns < 3:
                unknowns += 1
                log('recovery_wait', gateway=name, status=current, check=unknowns)
                sleep(10)
                current = status(query(), name)
            if current != 'down':
                log('recovery_stop', gateway=name, status=current)
                return
            unknowns = 0
            try:
                success = action(interface)
            except (OSError, subprocess.TimeoutExpired):
                success = False
            log('reconfigure', gateway=name, interface=interface, attempt=attempt, success=success)
            if attempt < cfg['max_recovery_attempts']:
                sleep(cfg['recovery_interval'])
        log('recovery_stop', gateway=name, reason='attempt_limit')


def spawn(command, name=None):
    args = [PYTHON, HELPER, command] + ([name] if name else [])
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)


def main():
    os.umask(0o077)
    syslog.openlog('wan-watchdog')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['monitor', 'boot', 'recover-event', 'recover',
                                          'worker', 'retry-blocked', 'status', 'check-config'])
    parser.add_argument('gateways', nargs='?', default='')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('must run as root')
    db = None
    try:
        cfg = configuration()
        if args.command == 'check-config':
            print('Configuration valid; interface associations and API credentials not live-tested.')
            return 0
        names = list(cfg['gateways'])
        if args.command in ('monitor', 'recover-event', 'recover'):
            if len(args.gateways) > 4096:
                raise ValueError('gateway argument too long')
            names = list(dict.fromkeys(args.gateways.split(','))) if args.gateways else []
            if any(not NAME.fullmatch(name) for name in names):
                raise ValueError('invalid gateway argument')
            for name in names:
                if name not in cfg['gateways']:
                    log('ignore', gateway=name, reason='not_configured')
            names = [name for name in names if name in cfg['gateways']]
        if args.command in ('recover-event', 'recover'):
            if not cfg['recovery_enabled']:
                return 0
            if args.command == 'recover-event':
                data = snapshot() if names else {}
                for name in names:
                    if cfg['gateways'][name] and status(data, name) == 'down':
                        spawn('recover', name)
            elif len(names) == 1:
                recover(cfg, names[0])
            else:
                raise ValueError('recover requires exactly one configured gateway')
            return 0
        db = database()
        if args.command == 'boot':
            if db.execute('SELECT 1 FROM events WHERE blocked=0 LIMIT 1').fetchone():
                spawn('worker')
            time.sleep(cfg['boot_delay'])
        if args.command in ('monitor', 'boot'):
            if observe(db, names):
                spawn('worker')
        elif args.command == 'worker':
            deliver(db, cfg)
        elif args.command == 'retry-blocked':
            with db:
                db.execute('UPDATE events SET blocked=0,due=0 WHERE blocked=1')
            spawn('worker')
        elif args.command == 'status':
            print(json.dumps(dict(pending=db.execute('SELECT count(*) FROM events').fetchone()[0],
                                  blocked=db.execute('SELECT count(*) FROM events WHERE blocked=1').fetchone()[0])))
        return 0
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        log('error', command=args.command, error_type=type(error).__name__)
        print('wan-watchdog: failed; inspect protected configuration and system logs', file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == '__main__':
    raise SystemExit(main())
