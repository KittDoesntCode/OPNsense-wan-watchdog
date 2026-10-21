# OPNsense WAN Watchdog

Event-driven recovery/alerts: no cron, custom pings, continuous status polling or idle
worker. The three existing hooks use one helper/configuration. Native validation is
still required before production use (TESTING.md).

## Design

50-wan-watchdog starts a bounded, per-interface recovery loop only for exact down.
60-wan-pushover observes affected configured gateways once. The boot hook resumes
pending delivery and performs one delayed check. Healthy observations spawn no delivery
worker; delivery sleeps/retries only while alerts remain, then exits.

SQLite is kept solely for atomic observation+pending-alert updates, ordering and
restart/reboot persistence; no database server. Recovery no longer uses it. State:
`/var/db/opnsense-wan-watchdog/state.sqlite3`, requiring persistent storage. Previous
dev queues are compatible; the unused recovery table can remain. Shared code avoids
duplicated send logic; root-only configuration avoids duplicated credentials.

Healthy first observation (none) silently seeds baseline; other first observations
and changes queue timestamped alerts. Transient failures retry after 10/20/30/60 seconds.
Per-gateway ordering is preserved. New events during a retry sleep may wait up to
60 seconds plus an in-flight request. HTTP 200 + integer API status 1 acknowledges.
Curl/malformed replies, HTTP 408/425/429/5xx retry; other HTTP errors, missing curl or
API rejection retain/block that gateway for correction, without blocking others.
Capacity 10,000; full queues roll back observations. No expiry. Duplicate delivery is
possible after a lost response/crash. API acceptance does not prove device display.

Deliberate limits: no periodic missing-monitor reconciliation or crash detector. A
killed worker resumes on another observation, boot or manual worker command. Unobserved
short states cannot be reconstructed; no Internet/DNS means no immediate remote alert.

## Configuration

Dependencies: /bin/sh, Python 3.8+ standard library with sqlite3/fcntl/syslog, curl,
pluginctl and configctl. No pip packages or scheduler registration.

Configuration: `/usr/local/etc/wan-watchdog.json`, root:wheel mode 600. Set real
30-character app/user or group keys. Example placeholders deliberately fail.
Gateways map exact monitor names to VERIFIED logical interface tokens, or null for
alert-only. `"SPECTRUM_GW": "wan"` is correct only if Spectrum really uses wan; this
is not old-name-to-new-name mapping. Configure every expected gateway.

Defaults: recovery_enabled=false, boot_delay=90, recovery_interval=70,
max_recovery_attempts=60, title="OPNsense Gateway Alert", device/sound empty, priority=0
(allowed -2..1). Remove prior dev source_ipv4/queue_limit fields; unknown fields fail.
Normal OPNsense routing/DNS failover is used, not manual/oldest-healthy WAN selection.

Only down resets. none/force_down/degradation stop recovery. Unknown/missing data gets
three 10-second rechecks, then stops without claiming recovery. Query failures stop
safely; failed resets count. Interval is additional to query/action time. A later event
may start a fresh bounded loop. Independent locks prevent overlapping interface loops.
A configctl timeout does not undo work already queued in configd.

## Install / upgrade

Use reviewed source and console access. Back up hooks/credentials privately (directory
700; files 600). Disable hooks and wait for workers. If prior dev was installed, FIRST
disable its cron job, remove its installed actions_wanwatchdog.conf and restart configd.
Preserve the SQLite queue. The retired repository action file is only a migration notice;
do not install it. Run from the checkout:

```sh
/usr/local/bin/python3 -c 'import sqlite3, fcntl, syslog'
/usr/local/bin/curl --version
/usr/local/sbin/pluginctl -r return_gateways_status
/usr/local/bin/python3 -m unittest discover -s tests -v
install -d -o root -g wheel -m 755 /usr/local/libexec
install -o root -g wheel -m 700 wan_watchdog.py /usr/local/libexec/wan_watchdog.py
# FIRST INSTALL ONLY: preserve/edit real JSON on subsequent upgrades.
install -o root -g wheel -m 600 wan-watchdog.example.json /usr/local/etc/wan-watchdog.json
vi /usr/local/etc/wan-watchdog.json
/usr/local/bin/python3 /usr/local/libexec/wan_watchdog.py check-config
install -o root -g wheel -m 700 50-wan-watchdog /usr/local/etc/rc.syshook.d/monitor/50-wan-watchdog
install -o root -g wheel -m 700 60-wan-pushover /usr/local/etc/rc.syshook.d/monitor/60-wan-pushover
install -o root -g wheel -m 700 20-wan-pushover-bootcheck /usr/local/etc/rc.syshook.d/start/20-wan-pushover-bootcheck
```

Keep recovery disabled until target tests pass. check-config checks values/permissions,
not live credentials or interface associations. No cron/configd registration needed.

## Operations / safety

Commands: status (counts), worker (resume), retry-blocked (after fixing permanent errors),
boot (delayed observation; can send real alerts). Invoke with:
`/usr/local/bin/python3 /usr/local/libexec/wan_watchdog.py <command>`.
All helper logs use wan-watchdog. Retest dependencies/hooks after OPNsense upgrades.

Verified HTTPS; connection/transfer/process limits 5/15/20 seconds. Credentials use
stdin, not process arguments. Raw API/stderr bodies are not logged. Curl config/proxy
overrides are disabled. Private state, validated identifiers, duplicate-key checks and
kernel locks protect processing. Never delete live locks or edit a live database.
Modem reachability is not upstream Internet health; reset success is not recovery proof.

Rollback: disable hooks, wait for workers, restore all three originals/config root:wheel
700/600. Preserve pending SQLite state; original scripts cannot deliver it or convert
it to .last files. Local tests do not certify physical recovery, production readiness or
CPU/I/O cost. Native tests/benchmarks remain outstanding. LICENSE unchanged.
