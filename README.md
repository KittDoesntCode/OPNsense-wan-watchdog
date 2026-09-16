# OPNsense WAN Watchdog

A small collection of FreeBSD `sh` scripts for OPNsense systems that need more proactive WAN recovery and notification behavior than the standard gateway-monitoring workflow provides.

The scripts use OPNsense syshooks to respond to gateway-monitor events and post-startup events. Together, they can:

- Attempt to recover a WAN interface when its monitored gateway is `down`.
- Send Pushover notifications when a monitored gateway status changes.
- Send a delayed post-boot Pushover alert when a gateway remains unavailable after OPNsense starts.
- Support multiple WAN gateways such as `WAN1_GW`, `WAN2_GW`, and future gateways such as `WAN3_GW`.

These scripts are intended for administrators who understand their OPNsense gateway, failover, routing, and interface configuration. Test them in your own environment before relying on them for production alerting or automated WAN recovery.

> **Security note:** The Pushover notification scripts contain an application API token and a user or group key. Install the scripts as `root:wheel` with mode `700` so only root can read or execute them.

## Scripts

| Script | Install location | Purpose |
|---|---|---|
| `50-wan-watchdog` | `/usr/local/etc/rc.syshook.d/monitor/50-wan-watchdog` | Reconfigures a mapped WAN interface while its monitored gateway remains exactly `down`. |
| `60-wan-pushover` | `/usr/local/etc/rc.syshook.d/monitor/60-wan-pushover` | Sends a Pushover notification when an affected gateway’s status changes. |
| `20-wan-pushover-bootcheck` | `/usr/local/etc/rc.syshook.d/start/20-wan-pushover-bootcheck` | Waits after boot, checks every reported gateway, and alerts when a gateway is still not up. |

## Prerequisites

Before installing any script:

1. Connect to OPNsense as `root` through SSH or the local console.

2. Confirm that gateway monitoring is configured and enabled in OPNsense.

3. Confirm that your monitored gateways appear in:

   ```sh
   pluginctl -r return_gateways_status
   ```

   Example:

   ```json
   {
     "dpinger": {
       "WAN1_GW": {
         "status": "none"
       },
       "WAN2_GW": {
         "status": "down"
       }
     }
   }
   ```

4. Confirm that `curl` is installed:

   ```sh
   which curl
   ```

   Expected output:

   ```text
   /usr/local/bin/curl
   ```

   `curl` is used both to download these scripts from GitHub and by the Pushover scripts to send HTTPS API requests.

5. For Pushover alerts, create a Pushover application and obtain:

   - An **application API token**
   - A **user key** or **group key**

6. Ensure scripts use Unix line endings. Windows CRLF line endings can prevent FreeBSD shell scripts from executing properly.

## Installation method

The recommended workflow is:

1. Download the script to `/tmp` using `curl`.
2. Review and configure it locally using `vi`.
3. Install it with `install`, which applies the correct owner, group, and permissions in one command.
4. Remove the temporary downloaded copy.

Do **not** pipe a remotely downloaded script directly into `sh`. Always inspect and configure it before installing it into an OPNsense syshook directory.

The raw GitHub URLs below assume:

- Repository owner: `KittDoesntCode`
- Repository: `OPNsense-wan-watchdog`
- Default branch: `main`
- Scripts are stored in the repository root

If your default branch or script location differs, adjust the URL before downloading.

## Status terminology

The scripts use raw OPNsense gateway status values internally for state tracking and duplicate suppression. Pushover alerts use more readable text.

| Raw OPNsense status | Pushover wording | Meaning |
|---|---|---|
| `none` | `up` | The gateway is considered healthy/up. |
| `down` | `down` | The gateway monitor considers the gateway unavailable. |
| `force_down` | `forced down` | The gateway has been administratively forced down. |
| `unknown` | `unknown` | The script could not determine a usable gateway status. |
| Any other value | Original value | The script reports the raw returned status. |

---

## 50-wan-watchdog

### Purpose

`50-wan-watchdog` reacts to OPNsense gateway-monitor events for configured gateway/interface pairs.

When a configured gateway is in the exact state `down`, it runs:

```sh
configctl interface reconfigure <interface>
```

The script repeats the interface reconfigure request once per minute until the gateway is no longer exactly `down`.

It does not attempt interface recovery when a gateway status is `force_down`, `none`, or another non-`down` state.

Each configured interface receives its own lock directory, which allows independent recovery loops for multiple WANs without allowing duplicate recovery loops for the same interface.

> **Caution:** This script can reconfigure WAN interfaces. Test it carefully, especially if you administer OPNsense remotely through the WAN interface being reconfigured.

### Download, configure, and install

Download the script to `/tmp`:

```sh
cd /tmp

/usr/local/bin/curl --fail --location --remote-name \
  https://raw.githubusercontent.com/KittDoesntCode/OPNsense-wan-watchdog/main/50-wan-watchdog
```

Review and configure it before installing:

```sh
vi /tmp/50-wan-watchdog
```

Find the `GATEWAY_INTERFACE_MAP` block and add your gateway/interface pairs.

Example for two WAN interfaces:

```sh
GATEWAY_INTERFACE_MAP='
WAN1_GW:wan
WAN2_GW:wan2
'
```

Example with a future third WAN interface:

```sh
GATEWAY_INTERFACE_MAP='
WAN1_GW:wan
WAN2_GW:wan2
WAN3_GW:wan3
'
```

The gateway name before the colon must exactly match the gateway name returned by:

```sh
pluginctl -r return_gateways_status
```

The interface token after the colon must be the OPNsense interface identifier accepted by:

```sh
configctl interface reconfigure <interface>
```

Common interface tokens are often `wan`, `wan2`, and `wan3`, but verify your local OPNsense interface assignments before enabling automated recovery.

Optional recovery controls near the top of the script:

```sh
RETRY_INTERVAL_SECONDS=60
POST_RECONFIGURE_SETTLE_SECONDS=10
MAX_ATTEMPTS=60
```

| Setting | Purpose |
|---|---|
| `RETRY_INTERVAL_SECONDS` | Delay between recovery attempts while the gateway remains exactly `down`. |
| `POST_RECONFIGURE_SETTLE_SECONDS` | Delay after each interface reconfigure request before the next status check. |
| `MAX_ATTEMPTS` | Maximum consecutive recovery attempts before the script stops. |

Install the configured script:

```sh
install -o root -g wheel -m 700 \
  /tmp/50-wan-watchdog \
  /usr/local/etc/rc.syshook.d/monitor/50-wan-watchdog

rm -f /tmp/50-wan-watchdog
```

Verify the installation:

```sh
ls -l /usr/local/etc/rc.syshook.d/monitor/50-wan-watchdog
```

Expected mode and ownership:

```text
-rwx------  1 root wheel  ... 50-wan-watchdog
```

### Test it

Check the current gateway state:

```sh
pluginctl -r return_gateways_status
```

Manually invoke the watchdog for one gateway:

```sh
/usr/local/etc/rc.syshook.d/monitor/50-wan-watchdog WAN1_GW
```

Manually invoke it for more than one affected gateway:

```sh
/usr/local/etc/rc.syshook.d/monitor/50-wan-watchdog WAN1_GW,WAN2_GW
```

Watch watchdog logs:

```sh
tail -f /var/log/system/latest.log | grep wan-watchdog
```

The watchdog also writes per-gateway status files under:

```text
/var/run/opnsense-wan-watchdog/
```

---

## 60-wan-pushover

### Purpose

`60-wan-pushover` reacts to gateway-monitor syshook events and sends Pushover notifications when a gateway’s status changes.

For each gateway name provided by the OPNsense monitor event, it reads the current status from:

```sh
pluginctl -r return_gateways_status
```

It compares the current status with the gateway’s last cached status and sends a Pushover alert only when a state transition occurs.

Example alerts:

```text
Gateway WAN2_GW changed status from up to down on OPNsense.
Gateway WAN2_GW changed status from down to up on OPNsense.
Gateway WAN2_GW changed status from up to forced down on OPNsense.
```

The script suppresses duplicates. If a gateway remains `down`, it does not send repeated alerts until its status changes.

### First-observation behavior

The script stores state under:

```text
/var/run/opnsense-wan-pushover/
```

If a gateway does not have a `.last` state file:

- `none` (`up`) is stored as a healthy baseline and does not send an alert.
- Any non-`none` status, including `down`, `force_down`, `unknown`, or another future status, is stored and sends an alert immediately.

This means an already-down gateway can still alert after reboot, first installation, or state-cache cleanup.

### Download, configure, and install

Download the script:

```sh
cd /tmp

/usr/local/bin/curl --fail --location --remote-name \
  https://raw.githubusercontent.com/KittDoesntCode/OPNsense-wan-watchdog/main/60-wan-pushover
```

Review and configure it before installing:

```sh
vi /tmp/60-wan-pushover
```

Set these required Pushover values:

```sh
APP_TOKEN="REPLACE_WITH_YOUR_PUSHOVER_APP_TOKEN"
USER_KEY="REPLACE_WITH_YOUR_PUSHOVER_USER_OR_GROUP_KEY"
```

Optional settings:

```sh
DEVICE_NAME=""
PUSHOVER_PRIORITY="0"
PUSHOVER_SOUND=""
PUSHOVER_TITLE_PREFIX="OPNsense Gateway Alert"
```

| Setting | Purpose |
|---|---|
| `DEVICE_NAME` | Leave blank to notify all eligible devices. Set a Pushover device name to target one device. |
| `PUSHOVER_PRIORITY` | Pushover message priority. Default is `0` for normal priority. |
| `PUSHOVER_SOUND` | Leave blank to use the Pushover account default. Set a valid Pushover sound name to override it. |
| `PUSHOVER_TITLE_PREFIX` | Text shown before the gateway name in the Pushover notification title. |

Install the configured script:

```sh
install -o root -g wheel -m 700 \
  /tmp/60-wan-pushover \
  /usr/local/etc/rc.syshook.d/monitor/60-wan-pushover

rm -f /tmp/60-wan-pushover
```

Verify the installation:

```sh
ls -l /usr/local/etc/rc.syshook.d/monitor/60-wan-pushover
```

Expected mode and ownership:

```text
-rwx------  1 root wheel  ... 60-wan-pushover
```

### Test it

Check the current status:

```sh
pluginctl -r return_gateways_status
```

Manually invoke the script for a gateway:

```sh
/usr/local/etc/rc.syshook.d/monitor/60-wan-pushover WAN2_GW
```

Inspect the Pushover state cache:

```sh
ls -la /var/run/opnsense-wan-pushover/
cat /var/run/opnsense-wan-pushover/WAN2_GW.last
```

To test first-observation behavior again for one gateway, remove only that gateway’s state file:

```sh
rm -f /var/run/opnsense-wan-pushover/WAN2_GW.last
```

Then invoke the script while the gateway is in a non-`none` state. It should immediately send an alert.

Watch logs:

```sh
tail -f /var/log/system/latest.log | grep wan-pushover
```

Useful log messages include:

```text
gateway=WAN2_GW action=alert transition=none->down
action=notify result=success request=<pushover-request-id>
```

---

## 20-wan-pushover-bootcheck

### Purpose

`20-wan-pushover-bootcheck` runs after OPNsense startup. It waits for gateway monitoring and network services to settle, then checks every gateway returned by:

```sh
pluginctl -r return_gateways_status
```

If a gateway is still not `none` (`up`) after the configured delay, the script sends a Pushover alert.

This closes an alerting gap that can occur when a WAN gateway is already unavailable while OPNsense starts and therefore does not create a later monitor-transition event.

The bootcheck shares the same state directory as `60-wan-pushover`:

```text
/var/run/opnsense-wan-pushover/
```

This reduces duplicate alerts when both scripts observe the same gateway status.

### Download, configure, and install

Download the script:

```sh
cd /tmp

/usr/local/bin/curl --fail --location --remote-name \
  https://raw.githubusercontent.com/KittDoesntCode/OPNsense-wan-watchdog/main/20-wan-pushover-bootcheck
```

Review and configure it before installing:

```sh
vi /tmp/20-wan-pushover-bootcheck
```

Configure the same Pushover settings used in `60-wan-pushover`:

```sh
APP_TOKEN="REPLACE_WITH_YOUR_PUSHOVER_APP_TOKEN"
USER_KEY="REPLACE_WITH_YOUR_PUSHOVER_USER_OR_GROUP_KEY"

DEVICE_NAME=""
PUSHOVER_PRIORITY="0"
PUSHOVER_SOUND=""
PUSHOVER_TITLE_PREFIX="OPNsense Gateway Alert"
```

Set the post-boot delay:

```sh
BOOT_DELAY_SECONDS="90"
```

A value of `90` seconds is a reasonable starting point. If your WAN interface, modem, DHCP lease, PPPoE connection, VLAN, or gateway monitoring service takes longer to settle after boot, increase it to `120` seconds.

Install the configured script:

```sh
install -o root -g wheel -m 700 \
  /tmp/20-wan-pushover-bootcheck \
  /usr/local/etc/rc.syshook.d/start/20-wan-pushover-bootcheck

rm -f /tmp/20-wan-pushover-bootcheck
```

Verify the installation:

```sh
ls -l /usr/local/etc/rc.syshook.d/start/20-wan-pushover-bootcheck
```

Expected mode and ownership:

```text
-rwx------  1 root wheel  ... 20-wan-pushover-bootcheck
```

### Test without rebooting

Run the script manually:

```sh
/usr/local/etc/rc.syshook.d/start/20-wan-pushover-bootcheck
```

The script waits for `BOOT_DELAY_SECONDS` before querying gateway status.

For a short temporary test, edit:

```sh
BOOT_DELAY_SECONDS="90"
```

and temporarily change it to:

```sh
BOOT_DELAY_SECONDS="5"
```

Run the script manually, confirm the expected alert behavior, then restore the normal delay before relying on it in production.

Watch logs:

```sh
tail -f /var/log/system/latest.log | grep wan-pushover-bootcheck
```

---

## Logs and state files

All scripts use the FreeBSD `logger` utility and write operational messages to the OPNsense system log.

Watch all script activity:

```sh
tail -f /var/log/system/latest.log | egrep 'wan-watchdog|wan-pushover|wan-pushover-bootcheck'
```

Watch only watchdog activity:

```sh
tail -f /var/log/system/latest.log | grep wan-watchdog
```

Watch Pushover-related activity:

```sh
tail -f /var/log/system/latest.log | egrep 'wan-pushover|wan-pushover-bootcheck'
```

Runtime state is intentionally stored under `/var/run/`:

```text
/var/run/opnsense-wan-watchdog/
/var/run/opnsense-wan-pushover/
```

The `/var/run` filesystem is normally cleared during reboot. This is intentional: the scripts rebuild their state from current gateway status after startup.

## Updating scripts

When updating a script, download the updated version to `/tmp`, review and configure it with `vi`, then install it over the existing file.

For example, to update `60-wan-pushover`:

```sh
cd /tmp

/usr/local/bin/curl --fail --location --remote-name \
  [https://raw.githubusercontent.com/KittDoesntCode/OPNsense-wan-watchdog/main/60-wan-pushover](https://raw.githubusercontent.com/KittDoesntCode/OPNsense-wan-watchdog/main/60-wan-pushover)

vi /tmp/60-wan-pushover

install -o root -g wheel -m 700 \
  /tmp/60-wan-pushover \
  /usr/local/etc/rc.syshook.d/monitor/60-wan-pushover

rm -f /tmp/60-wan-pushover
```

> **Important:** Downloading a repository update can overwrite your local Pushover credentials and local configuration changes. Before updating, record or back up your local settings. After editing the downloaded copy, verify that your `APP_TOKEN`, `USER_KEY`, gateway/interface mappings, and delay values are correct before running `install`.

## OPNsense upgrades

The scripts are installed in OPNsense syshook directories rather than modifying OPNsense-provided system scripts. This is intended to make them more resilient across normal reboots and upgrades.

After each OPNsense upgrade:

1. Confirm each script still exists and is executable:

   ```sh
   ls -l /usr/local/etc/rc.syshook.d/monitor/50-wan-watchdog
   ls -l /usr/local/etc/rc.syshook.d/monitor/60-wan-pushover
   ls -l /usr/local/etc/rc.syshook.d/start/20-wan-pushover-bootcheck
   ```

2. Confirm `curl` still exists:

   ```sh
   which curl
   ```

3. Confirm gateway status output still has the expected structure:

   ```sh
   pluginctl -r return_gateways_status
   ```

4. Perform a controlled test of one monitored gateway and verify:

   - Gateway-monitor logs appear.
   - The watchdog behaves as expected, if installed.
   - Pushover transition alerts arrive.
   - The delayed bootcheck runs after reboot or a controlled manual test.

## Troubleshooting

### Pushover script ran but no notification arrived

Watch logs:

```sh
tail -f /var/log/system/latest.log | egrep 'wan-pushover|wan-pushover-bootcheck'
```

A successful Pushover request includes:

```text
action=notify result=success request=<pushover-request-id>
```

If you see:

```text
action=notify result=transport_error
```

check:

- Internet connectivity and DNS resolution from OPNsense.
- Firewall rules that may block OPNsense itself from reaching `api.pushover.net` over HTTPS.
- That `/usr/local/bin/curl` exists and can run.
- TLS certificate validation.
- The configured Pushover API URL.

If you see:

```text
action=notify result=api_error
```

check:

- Pushover application API token.
- Pushover user or group key.
- Requested priority.
- Device name.
- Sound name.

### No alert for a repeated down state

This is expected. The scripts alert on state changes and suppress duplicates.

Inspect the cached state:

```sh
cat /var/run/opnsense-wan-pushover/WAN2_GW.last
```

To test first-observation behavior for one gateway:

```sh
rm -f /var/run/opnsense-wan-pushover/WAN2_GW.last
```

Then invoke the Pushover script while the gateway is in a non-`none` state, or cause a real gateway-monitor event.

### No automatic monitor-hook alert

Confirm the script is executable and in the exact expected location:

```sh
ls -l /usr/local/etc/rc.syshook.d/monitor/60-wan-pushover
```

Manually invoke it for troubleshooting:

```sh
/usr/local/etc/rc.syshook.d/monitor/60-wan-pushover WAN2_GW
```

Then inspect the system log for:

```text
action=start
action=observed
action=alert
action=notify result=success
```

### Gateway watchdog does not reconfigure an interface

Check the gateway/interface mapping in `50-wan-watchdog`:

```sh
vi /usr/local/etc/rc.syshook.d/monitor/50-wan-watchdog
```

Verify that:

- The gateway name exactly matches `pluginctl -r return_gateways_status`.
- The mapped interface token is valid for `configctl interface reconfigure`.
- The gateway is in the exact raw status `down`.
- The per-interface recovery lock is not already active.

Watch the watchdog log:

```sh
tail -f /var/log/system/latest.log | grep wan-watchdog
```

## AI assistance

Perplexity AI was used to assist with coding and troubleshooting. Review, test, and adapt these scripts for your own network before relying on them in a production firewall environment.
