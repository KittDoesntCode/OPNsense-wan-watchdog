# Validation and production release gate

## Executed in this change set

- Restored the exact helper committed on dev; Git blob SHA verified against
  e89c4bba02cf29f89e0b91b5faabf27583b0d8a7 before testing integration.
- 31 repository test methods, including parameterized fault/input cases, pass.
  Ten consecutive runs: 310 test-method executions.
- Python compilation and sh -n for all three hooks pass.
- CI YAML/configd INI structure validated locally, not on native configd.
- Tests exercise real SQLite/locking/concurrency and a fake transport subprocess;
  pluginctl/configctl/Pushover are mocked. No real credentials or resets used.
- This checked-in suite groups related cases; the earlier 55-method draft suite
  was not committed. Counts refer to this exact retained suite, not the old draft.
- Linux sandbox only. ShellCheck unavailable. CI has not run before commit.

## Required before production approval (not yet executed)

1. Run this suite on the target FreeBSD/OPNsense Python/SQLite build.
2. Verify executable paths, config ownership/mode, expected gateways and exact
   gateway-to-logical-interface associations.
3. Verify configd registration and every-minute Cron execution.
4. With recovery disabled, verify real API acceptance and actual device receipt.
5. In an authorized maintenance window with console access: failed delivery then
   outage/recovery ordering, DNS/WAN failover, worker interruption and reboot persistence.
6. If source_ipv4 is enabled, packet-capture the path and validate DHCP address changes.
7. Separately enable recovery and validate cooldown, exact-down action, unavailable
   monitor behavior, administrative force-down and maximum attempt budget.
8. Verify protected backup and complete rollback.

No green local test run certifies physical WAN recovery or production readiness.
