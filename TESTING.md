# Simplified implementation validation

Executed in a Linux sandbox: 26 unittest methods (including parameterized failures),
repeated 10 times; Python compilation; wrapper shell syntax; retired configd INI parse.
Tests use real SQLite transactions, concurrency and kernel locks; a fake curl process
checks the stdin/response contract. No real credentials, network calls or interface resets.
The tests explicitly check no worker/sleep for an empty queue, no delivery spawn on a
healthy event, no gateway query when recovery is disabled, bounded unknown-data grace,
and release of the worker lock before the empty-queue transaction commits.

Native FreeBSD/OPNsense, actual Pushover receipt, WAN/DNS failover, power-loss persistence,
physical recovery and CPU/memory/I/O benchmarking have NOT been executed. ShellCheck
is unavailable. CI results are not claimed before the workflow runs on this commit.

Before production: run the suite on the target, verify logical interface mappings and
permissions, remove the old dev cron/action if installed, test real notification delivery
with recovery disabled, then validate authorized outage/recovery with console access.
Test interrupted-worker restart, reboot queue retention and rollback. Only then enable
recovery. No guaranteed alert is possible without a working Internet/DNS path.
