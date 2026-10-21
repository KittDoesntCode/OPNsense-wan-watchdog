# OPNsense WAN Watchdog

Reliable gateway-transition notifications and opt-in WAN interface recovery.
This development upgrade replaces the three standalone scripts with thin hooks and
one shared helper. Install the complete set; replacing one hook alone is unsupported.
Native OPNsense validation is required before production enablement (see TESTING.md).

## Behavior

- `60-wan-pushover` observes affected configured gateways and queues transitions.
- `20-wan-pushover-bootcheck` starts a delayed check without blocking startup.
- `50-wan-watchdog` starts separate, bounded recovery work for configured gateways.
- A required periodic task reconciles expected gateways and retries pending messages.
- Healthy first observation (`none`) is a silent baseline. Other first observations
  and subsequent changes are queued with the original UTC timestamp and event ID.
- SQLite atomically commits observations and queued events. Failed delivery does not
  consume an alert. Recovery notifications cannot overtake outages for that gateway.
- State is private and persistent at `/var/db/opnsense-wan-watchdog/state.sqlite3`.
  Reboots preserve it only if that filesystem is persistent. No old `.last` migration.

Delivery uses verified HTTPS, a 5-second connection timeout, 15-second transfer
limit and 20-second subprocess ceiling. Retry eligibility delays are 10, 20, 30,
then 60 seconds; with one-minute scheduling, actual delivery waits for an eligible
tick. Monitor events can also launch delivery. One attempt per gateway is made per
worker invocation. No new attempts start after 45 seconds; a final attempt can add
20 seconds. Concurrent delivery/recovery workers use separate kernel locks.

Curl errors, malformed replies, HTTP 408/425/429 and 5xx retry. Other HTTP errors,
missing curl and API rejection block that gateway's oldest event for correction;
other gateways can progress. HTTP 200 plus integer JSON `status: 1` is required for
acceptance. Delivery is at least once: a lost response or crash after acceptance
can cause duplicates. API acceptance does not prove a device displayed the alert.

The queue defaults to 10,000 events. At capacity, observations roll back and log an
error rather than dropping pending events. Short transitions between samples or
while the queue is full cannot necessarily be reconstructed. No event expiry.

## Dependencies and security

Runtime: Python 3.8+ with `sqlite3`, Unix `fcntl`/`syslog`, curl, pluginctl and configctl.
All Python imports are standard library; no pip packages or database server.
SQLite availability depends on the installed Python build. Check it on the target.
`/bin/sh` is needed for the hooks. No extra recovery daemon/service is installed.

SQLite provides transactions, `fcntl` locking, `syslog` logging; the remaining
imports handle arguments, JSON, validation, files, subprocesses, time and encoding.
Curl is retained for explicit connection/transfer limits and optional source binding.
It receives secrets via stdin, not process arguments. TLS verification stays enabled;
curl configuration files and proxy-environment routing are disabled. Raw API/error
bodies are not logged. Configuration, state and helper files must be root-owned;
configuration/state must not be accessible to group/other users. Symlinks/hardlinks,
unsafe identifiers, duplicate mappings and invalid/unknown config fields are rejected.

## Configure

Copy `wan-watchdog.example.json` locally and set real 30-character app/user or group
keys. Place it at `/usr/local/etc/wan-watchdog.json`, root:wheel mode 600. Never commit
real credentials. Example placeholders deliberately fail validation.

`gateways` keys are exact names from `pluginctl -r return_gateways_status`.
Values are verified OPNsense logical interface tokens, or `null` for alert-only.
For example `"SPECTRUM_GW": "wan"` is correct ONLY if Spectrum actually uses `wan`.
This is NOT an old-gateway-name to new-gateway-name mapping. Verify assignments;
do not guess `wan2`, substitute a provider description, or use a gateway as a token.
Add every gateway you expect to monitor; unconfigured names are ignored.

Optional settings/defaults:

| Setting | Default |
|---|---|
| recovery_enabled | false (safe migration default) |
| boot_delay | 90 seconds |
| recovery_interval | 70 seconds minimum between reserved attempts |
| max_recovery_attempts | 60 per gateway until healthy/force-down |
| queue_limit | 10000 |
| device, sound | empty |
| priority | 0; allowed -2 through 1 (no emergency priority 2) |
| title | OPNsense Gateway Alert |
| source_ipv4 | empty (normal routing) |

Only exact `down` permits reconfiguration. `none`/`force_down` clear the recovery
budget; other degradation states do not reset an interface. Missing monitor data
becomes `monitor_unavailable`; invalid status becomes `unknown`. Both wait/recheck
without resetting, not falsely claim recovery. Complete query failures preserve
observed state. Recovery reserves attempts before execution (30-second subprocess
limit); failed attempts count. Exhausted budgets require healthy/force-down observation
to clear. A timeout does not undo work already initiated in configd.

`source_ipv4` optionally binds a CURRENT local IPv4 address. It is not dynamic DHCP
selection or an oldest-healthy-WAN algorithm, and does not bind DNS lookups. Verify
OPNsense default-gateway switching, DNS failover and force-gateway rules. Confirm
the actual path with packet capture before enabling binding. No script route changes.
A modem-address monitor tests that address, not necessarily upstream Internet access.
No remote notification is possible while every usable Internet/DNS path is unavailable.

## Install / upgrade

Use a reviewed checkout/archive, never download-and-execute. Schedule a maintenance
window with console/out-of-band access. Run from the reviewed source directory.

1. Check dependencies and run the isolated suite on the target (no real resets):

```sh
/usr/local/bin/python3 -c 'import sqlite3, fcntl, syslog'
/usr/local/bin/curl --version
/usr/local/sbin/pluginctl -r return_gateways_status
/usr/local/bin/python3 -m unittest discover -s tests -v
```

2. Disable the old hooks and wait for active recovery loops to finish. Back up all
three original hooks, install paths and credentials in a root-only directory (700;
files 600). Do not put backups under an active hook directory or in GitHub.

3. Install shared files BEFORE hooks. Do not overwrite an existing real JSON with
an example on subsequent upgrades:

```sh
install -d -o root -g wheel -m 755 /usr/local/libexec
install -o root -g wheel -m 700 wan_watchdog.py /usr/local/libexec/wan_watchdog.py
install -o root -g wheel -m 600 wan-watchdog.example.json /usr/local/etc/wan-watchdog.json
vi /usr/local/etc/wan-watchdog.json
/usr/local/bin/python3 /usr/local/libexec/wan_watchdog.py check-config
install -o root -g wheel -m 644 actions_wanwatchdog.conf /usr/local/opnsense/service/conf/actions.d/actions_wanwatchdog.conf
service configd restart
```

`check-config` validates values/permissions, not actual interface associations or
API credentials. Keep recovery disabled until target validation succeeds.

4. System > Settings > Cron: add `WAN watchdog reconcile and retry`, all schedule
fields `*`, no parameters, and apply. This every-minute job is REQUIRED for retries
without new events and missing-monitor reconciliation. Use the provided configd
action, not edits to a generated crontab. Verify registration on your OPNsense version.

5. Install all hooks with matching ownership/mode:

```sh
install -o root -g wheel -m 700 50-wan-watchdog /usr/local/etc/rc.syshook.d/monitor/50-wan-watchdog
install -o root -g wheel -m 700 60-wan-pushover /usr/local/etc/rc.syshook.d/monitor/60-wan-pushover
install -o root -g wheel -m 700 20-wan-pushover-bootcheck /usr/local/etc/rc.syshook.d/start/20-wan-pushover-bootcheck
```

6. Reconcile with recovery disabled, inspect counts and logs. This can send real
first-problem messages. Verify periodic execution before enabling recovery separately:

```sh
/usr/local/bin/python3 /usr/local/libexec/wan_watchdog.py tick
/usr/local/bin/python3 /usr/local/libexec/wan_watchdog.py status
tail -f /var/log/system/latest.log | grep wan-watchdog
```

All helper logs now use `wan-watchdog`, including notification/boot processing.
The old `wan-pushover`-only filter no longer covers them. Boot checking is delayed,
but periodic tasks/monitor hooks may observe earlier using the same queue.

## Operations and rollback

Logs distinguish curl exit code, HTTP errors, API rejection and acceptance, and
include gateway/event/attempt IDs. Correct blocked delivery, then explicitly release:

```sh
/usr/local/bin/python3 /usr/local/libexec/wan_watchdog.py retry-blocked
/usr/local/bin/python3 /usr/local/libexec/wan_watchdog.py worker
```

Do not delete live lock files or edit the database while workers run. Back up state
only after disabling hooks/scheduling and waiting for workers to finish.

Rollback: disable cron and hooks; wait for workers/recovery to finish; restore all
three original hooks root:wheel mode 700; remove the custom configd action and restart
configd. Preserve private JSON/queue for investigation: old scripts cannot deliver
that queue and restore the original delivery limitations. Do not translate database
contents into `.last` files. Repeat dependency/hook/cron checks after OPNsense upgrades.

## Release gate

See TESTING.md. Local/CI tests are isolated, not proof of native compatibility or
real WAN recovery. Do not enable production recovery until target checks pass.
License remains in LICENSE. AI assistance does not replace operator review.
