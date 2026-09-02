# RTK Base Station — Home Assistant Add-on (Unicore / u-blox)

Supervisor add-on (not strictly a HACS integration — see the note below),
originally built for the Unicore UM982 but **structured with a per-receiver
driver** (see the "Multi-receiver support" section below) — it now also
supports u-blox ZED-F9P/M8P, and can support others with a new module in
`drivers/`. What it does:
- reads the configured RTK GNSS receiver (`receiver_type`) over serial;
- sends RTCM corrections as an **NTRIP server** to one or more casters via
  RTKLIB `str2str` (the same role as RTKBase's `str2str_ntrip_A/B`
  services);
- exposes in Home Assistant via **MQTT Discovery**:
  - `sensor.fix_status` — Single / DGPS / Float / Fix / etc.
  - `sensor.satellites_in_use`
  - `sensor.estimated_accuracy` (meters, from NMEA GST)
  - `sensor.survey_in` — idle / running / done / error / **cancelled** (quick average, metric-level)
  - `button.start_survey_in` / `button.cancel_survey_in`
  - `sensor.survey_in_time_remaining` (seconds, updated every second while running)
  - `sensor.ppp_campaign` — idle / logging / processing / **waiting_for_products** / done / error / **cancelled**
  - `number.ppp_campaign_duration` (hours)
  - `button.start_ppp_campaign` / `button.cancel_ppp_campaign` — PPP-static processing over multi-hour raw logs (centimeter-level)
  - `sensor.ppp_campaign_time_remaining` (seconds, updated every second during the logging phase and during `waiting_for_products`)
  - `sensor.ppp_refinement_status` — idle / waiting_for_final / available / error (background daily check for more precise "final" IGS products after a campaign completes with "rapid" ones, see below)
  - `button.retry_ppp_computation` — if the campaign errors out after already downloading the IGS products (e.g. a bad `rnx2rtkp` config value), retries just that computation with the same downloaded products instead of forcing a full re-log + re-download (see below)
  - `button.reprocess_existing_logs` — runs a PPP-static computation over whatever raw log files are still on disk (up to `raw_log_retention_hours` back), with no new logging phase - a recovery option if a campaign's intermediate files were lost some other way (see below)
  - `number.manual_latitude` / `manual_longitude` / `manual_height`
  - `button.apply_manual_position`
  - `sensor.local_caster_connected_rovers` (only if `caster_enabled: true`)
  - `binary_sensor.device_connected` — ON/OFF, useful for automations/notifications
  - `sensor.last_data_received` (timestamp of the last valid NMEA data)
  - `sensor.rtcm_bitrate_in` (bps, from `str2str`'s real status)
  - `sensor.output_status_1/2/3/...` (one per configured caster/log/local
    relay: Connected / Waiting / Closed / Error, read from `str2str`'s
    actual status, not just whether the process is alive)
  - `sensor.str2str_diagnostics` (last error/status message from the
    individual outputs, e.g. a wrong password on the caster)
  - `sensor.configuration_error` (e.g. too many outputs configured, see
    below)
  - `sensor.last_computed_position` (timestamp + attributes with
    lat/lon/height/method/parameters, see below)
- optionally, acts as a **local NTRIP Caster** (port 2101/tcp) that rovers
  can connect to directly, in addition to or instead of external casters

It also exposes a **graphical panel in the Home Assistant sidebar**
(via Ingress) with a real-time skyplot: visible satellites positioned by
azimuth/elevation, colored green when used in the current fix, a badge with
fix status, satellites in use, estimated accuracy, HDOP/PDOP. No extra
configuration required: it appears automatically in the sidebar after
installation ("RTK Base").

## Multi-receiver support

The rest of the pipeline (RTKLIB `str2str`, NMEA parsing for
fix/satellites/skyplot, local NTRIP caster, PPP campaign) is already
generic: RTCM3 and NMEA-0183 are standard protocols, not vendor-specific.
The only genuinely receiver-dependent part is **how it's configured**
(enabling the right messages, setting the fixed base position) — isolated
in the `drivers/` package:

- `drivers/base.py` — documents the contract: each driver is a module that
  exposes `configure_rtcm(port, baud)`, `configure_nmea(port, baud)`,
  `set_rover_mode(port, baud)`, `set_fixed_base(port, baud, lat, lon, height)`.
- `drivers/unicore.py` — driver for Unicore UM980/UM982 (ASCII commands
  `log ... ontime ...` / `mode base|rover`).
- `drivers/ublox.py` — driver for u-blox ZED-F9P/F9R/M8P (binary UBX
  protocol: `UBX-CFG-MSG` to enable messages, `UBX-CFG-TMODE3`
  for rover/fixed base).
- `drivers/__init__.py` — registry; the add-on's `receiver_type` option
  selects which driver to use (`unicore_um98x` or `ublox_zedf9p`).

**To add another receiver** (e.g. Septentrio Mosaic-X5): create
`drivers/new_module.py` with the same four functions, register it in
`drivers/__init__.py` (`DRIVERS["new_module"] = new_module`), and add the
value to the `receiver_type` enum in `config.yaml`. No changes required
to the rest of the code (`main.py`, `nmea.py`, `ppp.py`, `caster.py`
stay unchanged).

The u-blox driver has only been validated at the code level: UBX
framing/checksum tested with a numeric round-trip (lat/lon/height correctly
encoded and decoded) and with a simulated ACK/NAK over a pty, but **not**
with a real ZED-F9P module — the RTCM3/NMEA message IDs and the TMODE3
layout come from public u-blox documentation and need to be confirmed on
your hardware (see the disclaimer at the top of `drivers/ublox.py`). To
help with verification, every command now explicitly waits for and logs
the module's UBX-ACK-ACK/NAK response, instead of sending "blind" as in
the first version — a `NAK` or `no response` log line is already a
reliable diagnostic signal. See **`../HARDWARE_VERIFICATION.md`** for the
full test protocol (driver, `str2str`, local caster, USB resilience,
survey-in/PPP) to follow with real hardware.

Entities, MQTT topics (`gnssbase/...`), device (`RTK Base Station`, with
`model` set at runtime to the selected driver's name) and log files
(`gnssbase_*.rtcm3`) are now generic, with no references to "UM982" —
consistent with supporting multiple receivers. Since this project hasn't
yet been installed on a real instance, this rebranding didn't have to
worry about breaking existing `unique_id`s/entities; if you change these
names again in the future on an already-in-use installation, the old
entities will remain "orphaned" in HA until you remove them manually.

## Relationship with the other files in the `um982/` folder

This add-on is one of three paths pursued for the same RTK base,
documented in the top-level folder:

- **`../ISTRUZIONI.md`** — manual procedure via Python scripts +
  ESP32 ([esp32-ntrip-DUO](https://github.com/designer2k2/esp32-ntrip-DUO/)).
  This add-on **entirely replaces** that procedure if you use Home
  Assistant: it internalizes `configure_um982.py` (in `drivers/unicore.py`),
  `ppp_process.py` (in `ppp.py`, invoked by the PPP campaign) and
  `set_um982_base.py` (manual position/survey-in/PPP campaign), and doesn't
  need the ESP32 because it already acts as a multi-caster NTRIP server.
- **`../RTKBASE_PROXMOX.md`** — alternative using RTKBase on a Proxmox VM.
  Covers the same use case (multi-caster + UM982 management) but outside
  Home Assistant, without the integrated skyplot panel.
- The standalone scripts (`../ppp_process.py`, `../configure_um982.py`,
  `../set_um982_base.py`) remain useful as an independent reference
  outside the add-on, for example to manually reprocess a raw log or to
  verify/compare a PPP result outside the container.

If you're starting from scratch and already use Home Assistant
Supervised/OS, this add-on is the most direct path: no need to follow
`ISTRUZIONI.md` step by step, this README is enough.

## Terminology note: HACS vs Add-on

This is a **Supervisor Add-on** (Docker container), not a HACS
integration. It's installed with the same "add Git repository" mechanism
you use with HACS, but from Home Assistant's native menu:
**Settings → Add-ons → Add-on Store → ⋮ (top right) → Repositories**,
pasting this repository's URL. It only works on
**Home Assistant OS** or **Supervised** installations — not on plain
"Home Assistant Container", which has no Supervisor.

## Prerequisites

- An MQTT broker **connected via Home Assistant's MQTT integration**
  (Settings → Devices & services → Integrations →
  MQTT). It's not enough for the broker to just be installed/started: the
  integration is what registers host/port/credentials in the service that
  Supervisor exposes to add-ons (`bashio::services mqtt`). This applies
  both to the official "Mosquitto broker" add-on and to an external
  broker (e.g. EMQX): without a configured integration, the add-on stays
  waiting (retrying every 10s, without crashing), showing
  `MQTT broker unreachable` in the logs.
- The GNSS receiver (UM982, u-blox, ...) connected via USB/serial to
  the host running Home Assistant.

## Configuration

In the add-on, "Configuration" tab:

```yaml
receiver_type: unicore_um98x   # or: ublox_zedf9p
rtcm_port: /dev/ttyUSB0
nmea_port: /dev/ttyUSB0
baudrate: 115200
survey_in_duration_sec: 300
ntrip_casters:
  - host: rtk2go.com
    port: 2101
    mountpoint: YOURMOUNTPOINT
    password: ""
  - host: private-caster.example.com
    port: 2101
    mountpoint: BASE1
    password: "password"
```

Note: no `user` field. RTKLIB, in the server/encoder role (`str2str -out
ntrips://...`), authenticates to the caster only with the **mountpoint
password** (NTRIP1 protocol `SOURCE <password> <mountpoint>`) — a
username would have no effect (verified by reading `reqntrip_s` in
RTKLIB's `src/stream.c`). For RTK2go, the mountpoint password is the one
chosen when the mountpoint itself was registered.

If `rtcm_port` and `nmea_port` are the same, the add-on still sends both
sets of configuration commands on the same port (RTCM3 + NMEA GGA/GST
coexist fine on the same serial stream for `str2str`, which only extracts
RTCM frames and ignores the rest).

### Multiple NTRIP casters at the same time

`ntrip_casters` is a list: from the add-on UI (Configuration tab) you can
add or remove entries with the +/- buttons, one for each caster you want
to send corrections to (e.g. RTK2go + a private caster + another one).
Entries with an empty `host` are ignored. Technically they all share a
single `str2str` instance that reads the serial port once and fans it out
to multiple `-out` targets (one per caster, plus one for the continuous
raw log used by the PPP campaign) — reading the same serial port from
separate `str2str` processes would corrupt the stream, which is why it's
important to always have a single process with multiple `-out` targets
rather than one process per caster.

**Non-obvious limit, verified in RTKLIB's source code**: `str2str`
supports at most **4 outputs total** (`MAXSTR=5` in `str2str.c`: 1 input +
4 outputs — a fifth `-out` is silently and unpredictably ignored, not with
an error). One of these slots is always taken by the continuous raw log,
and one by the local caster if `caster_enabled: true`. So the maximum
number of configurable external casters is:
- **3** if `caster_enabled: false`;
- **2** if `caster_enabled: true`.

If you exceed this limit, the add-on **won't start `str2str`** (to avoid
RTKLIB's undefined behavior) and publishes the reason in
`sensor.configuration_error` — the rest of the add-on (skyplot, survey-in,
etc.) keeps working normally in the meantime.

### Real caster connection status (not just "the process is alive")

Previously the only check was whether the `str2str` process was running —
if a caster rejected authentication, `str2str` stayed "alive" regardless,
with nothing flagging it. Now the add-on reads `str2str`'s stderr, which
periodically prints (every 5s) a real status line for each output (format
verified by actually compiling and running the binary, not inferred), and
translates it into `sensor.output_status_N` (one per configured
caster/log/local relay, in the same order they're configured) +
`sensor.str2str_diagnostics` with the exact error text when present (e.g.
a wrong password shows the caster's own error, not a generic "error").

### Acting as a caster for rovers (without an external caster)

By default the add-on only acts as a **client/uploader** toward the
external casters listed in `ntrip_casters` (like RTKBase's
`str2str_ntrip_A/B` services) — it doesn't respond to incoming
connections.

Setting `caster_enabled: true` makes the add-on also act as a minimal
**NTRIP Caster** (NTRIP v1/ICY handshake, with sourcetable and optional
Basic Auth) on port **2101/tcp**, exposed via the add-on's "Network"
tab (remappable to a different host port from there). Rovers connect
with:

```yaml
caster_mountpoint: "GNSSBASE"   # path rovers connect to: /GNSSBASE
caster_user: ""              # empty = no authentication required
caster_password: ""
caster_max_clients: 10       # concurrent rovers accepted, beyond this rejected with 503
```

It works in parallel with external casters: they all share the same
`str2str` instance, which besides the `-out ntrips://...` targets and the
file log also gets a `-out tcpcli://127.0.0.1:28101` toward a small
internal relay (`caster.py`), which redistributes the RTCM bytes to all
connected rovers. The number of connected rovers is exposed as
`sensor.local_caster_connected_rovers`.

**Hardening included**: after too many wrong passwords from the same IP
(5 attempts in 60s by default) the IP gets blocked for 5 minutes, even if
it then sends the correct password — making a brute-force of the
mountpoint password impractical. The maximum number of concurrently
connected rovers is configurable via `caster_max_clients` (default 10):
beyond the limit, new connections are rejected with `503`.

Known limits of this minimal implementation (fine for
personal/small-scale use, not for a high-traffic public caster):
- it handles a single mountpoint (the configured one), not a sourcetable
  with multiple stations;
- the anti-brute-force block is per-IP and in-memory (it resets if the
  add-on restarts) — it doesn't protect against an attack distributed
  across many IPs, and may incorrectly block multiple legitimate users
  behind the same NAT/public IP if they mistype the password too many
  times;
- no TLS encryption (plain NTRIP, like most "simple" casters — use a VPN
  or tunnel if you need to expose it beyond the LAN; not implemented in
  this pass, possible separate follow-up).

## What happens if the USB is not connected (or gets disconnected)

The add-on is resilient to a missing/disconnected USB, both at startup and
during operation:

- **At startup**, if `rtcm_port`/`nmea_port` don't exist yet (e.g. the USB
  isn't enumerated yet when the add-on starts), the add-on **waits**
  instead of exiting, retrying every 5 seconds until the port appears.
- **At runtime**, the thread reading NMEA detects both the port
  disappearing (device removed) and a prolonged silence (>15s with no
  data, even if the port still exists): in both cases it closes the
  connection and retries periodically, never letting the add-on process
  terminate.
- Status is visible in Home Assistant via
  `binary_sensor.device_connected` (ON/OFF) and
  `sensor.last_data_received` (timestamp of the last valid NMEA
  data) — you can build an automation on top of it (e.g. notify if it
  stays OFF for more than N minutes).
- `str2str` already has a separate watchdog (`watchdog_str2str`) that
  restarts it if it exits unexpectedly, independent of this mechanism.

Note: since no real hardware was available during development, this
resilience was verified by simulating the USB with a pty
(pseudo-terminal) — initial absence, appearing at runtime, and
disconnection during operation — not with a real USB-serial adapter. The
behavior with a real disconnected USB cable should be equivalent (the
kernel removes the `/dev/ttyUSB0` or `/dev/ttyACM0` device node), but it's
worth confirming on the first real test.

## Survey-In vs PPP Campaign: read this before using it

There are now **two ways** to fix the base's position, with very
different precision:

- **Quick Survey-In** (`button.start_survey_in`): average of
  standalone (single-point, non-differential) positions collected for
  `survey_in_duration_sec` seconds. Typical accuracy
  **meter/sub-meter level**. Useful for quick tests or non-critical
  installations, not for a production RTK reference.

- **PPP Campaign** (`button.start_ppp_campaign`): the add-on already
  continuously logs the raw RTCM stream to `/data/raw_logs` (with hourly
  rotation, retention configurable via `raw_log_retention_hours`).
  Starting the campaign, the add-on waits `ppp_duration_hours` hours, then
  automatically processes the files accumulated in that window with the
  same procedure as `ppp_process.py` (convbin → download IGS products →
  rnx2rtkp PPP-static) and applies the result as the fixed base. Expected
  **centimeter-level** accuracy with several hours of logs (the longer you
  log, the better it converges, especially for height). **This is the
  method to use for a permanent installation**, not the quick survey-in.

The PPP campaign result is also published in the "manual position"
fields, so it stays visible/reapplicable later via
`button.apply_manual_position`.

### Remaining time and cancellation

Both procedures expose a countdown
(`sensor.survey_in_time_remaining` / `sensor.ppp_campaign_time_remaining`,
in seconds, updated every second) and can be interrupted with
`button.cancel_survey_in` / `button.cancel_ppp_campaign`: the state moves
to `cancelled` and **no position is applied to the receiver**.

For the PPP campaign, cancellation works during `logging` and during
`waiting_for_products` (see below) — the two phases that can run for a
long time. It does not work during the brief, purely computational parts
of `processing` (RINEX conversion, then PPP once products are available):
interrupting those midway would risk leaving inconsistent temporary
files. In practice this isn't a limitation: those parts typically take a
few minutes each.

#### If IGS products aren't published yet: `waiting_for_products`

IGS precise products aren't available immediately: "final" products
(most precise) are published ~11-18 days after the observation date,
"rapid" products (the automatic fallback, see below) after ~17-41 hours.
If a campaign finishes logging and processing (RINEX conversion) but the
products for that date aren't out yet, `sensor.ppp_campaign` moves to
`waiting_for_products` instead of failing outright, and the campaign
retries the download automatically every hour. The wait is bounded by
`raw_log_retention_hours`: retrying past that point would be pointless,
since the source raw log will already have been deleted by the periodic
cleanup by then. `sensor.ppp_campaign_time_remaining` keeps counting down
during this phase too (time left until that bound), and
`button.cancel_ppp_campaign` still works.

In practice this means: for a campaign to complete automatically without
manual intervention, plan for it to keep the add-on's `raw_log_retention_hours`
comfortably larger than "campaign duration + expected product latency"
(the default 72h covers a same-day campaign followed by up to ~41h of
waiting for rapid products).

#### Getting the best possible precision later: `sensor.ppp_refinement_status`

Once the campaign completes with `sensor.ppp_campaign` = `done`, check
what tier was actually used (see the add-on log: `[ppp] completed: ...
(tiers used: {...})`). If it was `RAP` rather than `FIN` — the normal
case for a same-day/next-day campaign, since "final" products take
~11-18 days — the add-on automatically starts a **separate background
task** that checks once a day whether `FIN` products have since been
published for that campaign's date(s). This doesn't block a new
campaign from being started in the meantime (a new campaign starting
supersedes and stops it).

If/when `FIN` becomes available, `sensor.ppp_refinement_status` moves to
`available` and the refined (more precise) coordinates are published to
the same `number.manual_latitude` / `manual_longitude` / `manual_height`
fields used everywhere else in this add-on — **not applied to the
receiver automatically**: reapplying it remains a deliberate action via
`button.apply_manual_position`, consistent with how this add-on never
changes the base's live position without explicit user action. Values:
`idle` (nothing to refine, `FIN` was already used) / `waiting_for_final`
/ `available` / `error`.

#### If the final computation step fails: `button.retry_ppp_computation`

Once IGS products are successfully downloaded, only one step is left:
running `rnx2rtkp` (RTKLIB) on them. If that step itself fails - a bad
value in the internal PPP config being the only way this has happened so
far - the campaign moves to `error`, but **the already-downloaded
products and converted RINEX files are kept** instead of being deleted.
`button.retry_ppp_computation` retries just that computation, with the
same data, without re-logging (hours) or re-downloading. This state is
cleared (the button becomes a no-op) as soon as a new campaign starts,
since starting one already discards the old campaign's working files.

#### Recovering from a lost campaign: `button.reprocess_existing_logs`

The add-on always logs continuously to `/data/raw_logs` regardless of
whether a PPP campaign is active (that's what makes `raw_log_retention_hours`
meaningful in the first place - see above). If a campaign's *intermediate*
files get lost some other way than the computation failure covered by
`retry_ppp_computation` above (e.g. an add-on version before 0.2.20, or
simply never having started a campaign through the button at all),
`button.reprocess_existing_logs` runs the same PPP-static pipeline
(RINEX conversion → IGS products → `rnx2rtkp`) directly over whatever
raw log files are still retained on disk, going back up to
`raw_log_retention_hours` - **with no new logging phase**. It shares the
same `sensor.ppp_campaign` state machine (`processing` →
`waiting_for_products` if needed → `done`/`error`) and is mutually
exclusive with a normal campaign/retry (only one PPP operation runs at a
time); starting it discards any pending `retry_ppp_computation` state,
the same way starting a new campaign does.

### Backup of the computed position

Every time a position is fixed (survey-in, PPP campaign, or manual
application), the add-on saves a small JSON to `/data/position_backup.json`
with **the provenance**, not just the value: lat/lon/height, method
(`survey_in`/`ppp`/`manual`), date/time, receiver driver used, and
method-specific parameters (e.g. number of samples for survey-in,
hours/files for the PPP campaign). The same content is exposed as
`sensor.last_computed_position` (state = timestamp, attributes = the rest
of the data).

Why: the receiver might save the position in its own internal
configuration (if `saveconfig` succeeds), but if the container/add-on is
recreated from scratch, without this backup the add-on would no longer
remember *how* or *when* that position was computed — the "manual
position" fields would come back empty until you reset them by hand. At
startup, if the backup exists, it's automatically restored into the
"manual position" fields (memory/MQTT only: it is **not** automatically
sent back to the receiver — that remains a deliberate action via
`button.apply_manual_position`, if you decide to reapply it).

#### Permanent archive of the raw logs behind a PPP position

Whenever a PPP campaign (or `reprocess_existing_logs`/`retry_ppp_computation`,
see above) successfully computes and applies a position, the exact raw
log files that went into that computation are copied to
`/data/ppp_source_logs/<timestamp>/` - a directory `cleanup_raw_logs()`
never touches, so **these copies are never deleted automatically**,
regardless of `raw_log_retention_hours`. The continuous log in
`/data/raw_logs` still rotates out normally; this is a separate,
permanent copy of specifically the data that determined an actual
applied position, kept for provenance/reprocessing even long after the
original raw log would otherwise be gone. One archive per successful
computation - old ones aren't replaced or pruned by the add-on, so if
you run many campaigns over time and want to reclaim the space, that's a
manual cleanup (e.g. via the Samba share or Terminal/SSH add-on).

Note: the PPP campaign requires outbound internet access from the
container to download the IGS products (SP3/CLK/ANTEX) from BKG
(Bundesamt für Kartographie und Geodäsie, Germany - public, no
credentials required, verified with a real download). The previous
mirror, `files.igs.org`, no longer serves IGS products at all (its own
`readme.txt` says so) - every download through it would have failed;
this was found and fixed by testing a real PPP campaign against actual
hardware, see the changelog.

## Automated tests (runnable outside Home Assistant)

All the logic not tied to a real Supervisor/broker/hardware has a
`pytest` suite in `gnss_rtk_base/tests/`, designed to run locally or in CI
without needing Home Assistant, Docker, RTKLIB, or a physical receiver:

```bash
cd gnss_rtk_base
pip install -r requirements-dev.txt
pytest -v
```

What it covers (77 tests): NMEA parsing (GGA/GST/GSV/GSA), the Unicore and
u-blox drivers (including UBX-ACK/NAK parsing simulating a module that
responds ACK/NAK), the driver registry, the local NTRIP caster
(sourcetable, auth, byte relay), the PPP logic that doesn't require
external binaries (file selection, date/position parsing, download with
mocked mirrors), the MQTT Discovery entities, USB resilience (`main.py`)
simulated with a pty, the `str2str` 4-output limit and the `/dev/` path
fix — including a regression test for the bug where the `for caster in
ntrip_casters` loop shadowed the `caster` module (see this project's
changelog).

One test (`test_monitor_str2str_status_against_real_str2str_binary`) is a
**real integration** with the actual `str2str` binary (not simulated): it
only runs if `str2str` is in `PATH` (`pip install -r
requirements-dev.txt` doesn't install it — it must be compiled from
RTKLIB, see `Dockerfile`), otherwise it's automatically skipped
(`SKIPPED`), so the suite remains runnable anywhere without requiring
RTKLIB as a hard prerequisite.

What this suite does **not** cover (requires real hardware) — see instead
**`../HARDWARE_VERIFICATION.md`**:
- whether Unicore/u-blox commands are actually applied by a real module;
- whether `convbin`/`rnx2rtkp` (RTKLIB) behave as expected on data from a
  real receiver (str2str, on the other hand, is now also tested against a
  real binary, see above);
- whether a real field NTRIP client (SW Maps, Emlid Flow, etc.) connects
  correctly to the local caster, or a real external caster (RTK2go)
  accepts the corrections.

## Things to verify/adapt before real-world use

- **Selected driver's command syntax** (`drivers/unicore.py` or
  `drivers/ublox.py`): for Unicore, `mode base`, `mode rover`,
  `log ... ontime ...` are the most likely syntax for the UM982; for
  u-blox, the RTCM3/NMEA message IDs and TMODE3 layout come from public
  documentation. In both cases they need to be confirmed against your
  module's command/interface manual: check the add-on logs for any
  unrecognized or unapplied commands.
- ~~RTKLIB build paths~~ — **fixed and verified with a real Docker image
  build on Alpine** (`ghcr.io/home-assistant/amd64-base:3.19`, the same
  base used by Supervisor), not just on Debian/glibc: pinned to tag
  `v2.4.3-b34`, correct paths (`app/consapp/<tool>/gcc`). During the first
  real build on Alpine, a third bug surfaced (besides the two below), also
  fixed: `rnx2rtkp` also links against `lib/iers/gcc/iers.a` (a Fortran
  library for tidal corrections) and against `-lgfortran` — neither was
  the `gfortran` package installed, nor was that library built beforehand.
  Added `apk add gfortran` and a build step for `lib/iers/gcc` before
  str2str/convbin/rnx2rtkp; the image now builds and produces all three
  binaries, verified working inside the real container (not just an
  isolated build test).
- ~~`str2str` syntax with multiple `-out`~~ — **fixed and verified with a
  real binary compiled from the pinned tag**, not just read from the help
  text: the `-in stream [-out stream [-out stream...]]` syntax, the
  `serial://`, `file://`, `ntrips://`, `tcpcli://` formats, and the status
  line on stderr (now used for `sensor.output_status_N`) were actually
  observed by launching the binary with a pty as input. This found two
  real bugs, both fixed: (1) `serial://` doesn't want the `/dev/` prefix
  (str2str prepends it itself — passing the full path duplicated it, and
  `str2str` never started, **on every previous configuration of this
  add-on**); (2) `str2str` accepts **at most 4 total `-out`
  targets** (`MAXSTR=5` in the source), a limit now enforced explicitly
  (see above). Not yet verified: a real production caster (RTK2go or
  similar) — only a simulated local/unreachable caster — and the log
  file's hourly rotation (`%Y%m%d%h`), which wasn't triggered during this
  verification.
- **RTKLIB already implements a native caster role**, discovered by
  reading the source during this verification: `str2str -out
  ntripc://[user:passwd@][:port]/mntpnt[:srctbl]` does exactly what
  `caster.py` does in this add-on (accepts multiple connections, HTTP/1.1,
  sourcetable, authentication), contrary to what was claimed in an
  earlier phase of this project (it had been concluded — incorrectly —
  that RTKLIB didn't support the caster role at all). It hasn't been
  evaluated yet whether to replace `caster.py` with this native
  functionality: the advantage would be less custom code to maintain, the
  disadvantage is losing the connected-rovers count that `caster.py`
  currently exposes via MQTT (`str2str` doesn't seem to offer a direct way
  to report that externally).
- **Disk space**: the continuous raw log in `/data/raw_logs` grows over
  time up to the configured retention (`raw_log_retention_hours`, default
  72h). Size the retention according to the available space on the host.
- ~~Missing `init: false` in `config.yaml`~~ — **fixed**, found only
  during the first real deploy on a Home Assistant instance: without this
  field (absent = `true` by default), Supervisor wraps the container with
  its own init, which becomes PID 1 instead of `/init` (s6-overlay)
  already present in the base image, causing
  `s6-overlay-suexec: fatal: can only run as pid 1` at startup. Not
  reproducible with direct `docker build`/`docker run` (only with
  Supervisor's real orchestration) — a limitation of the verification
  done in this project: local builds and runs confirm the image builds
  and the Python code works, but not every Supervisor-specific convention
  (like this one).
- ~~Crash if the MQTT broker isn't ready yet~~ — **fixed**, found in the
  same real deploy: `self.mqtt.connect()` failed with an unhandled
  `ConnectionRefusedError` if no MQTT integration is connected to a
  broker, terminating the whole add-on. It now retries every 10s with a
  clear message in the logs, verified with a real build+run of the
  container without MQTT available.
- **Skyplot, "used" satellites**: the `used` flag compares PRNs read from
  GSA with those seen in GSV without distinguishing the constellation
  (NMEA numbering can overlap between constellations on multi-GNSS
  receivers). Fine for an indicative display; if you notice obvious
  inconsistencies, check how the UM982 numbers satellites in
  multi-constellation GSA sentences and adapt
  `nmea.parse_gsa`/`state.py` accordingly.
- **Local caster**: the handshake/sourcetable/auth/relay logic has been
  tested with a raw socket client (sourcetable, 401, ICY 200 OK, and byte
  forwarding all work), but not yet with a real field NTRIP client (e.g.
  SW Maps, Emlid Flow, u-center): if a rover app doesn't connect, check
  with Wireshark/tcpdump exactly what the client expects in the first
  exchange (some apps send `Ntrip-Version: Ntrip/2.0` or additional
  headers that this minimal implementation ignores).

## Files

- `repository.yaml` — repository manifest for the add-on store.
- `gnss_rtk_base/config.yaml` — add-on manifest (options, architectures).
- `gnss_rtk_base/icon.png` / `logo.png` — icon shown in the Add-on Store.
- `gnss_rtk_base/build.yaml` — base images per architecture.
- `gnss_rtk_base/Dockerfile` — image build (RTKLIB from source + Python).
- `gnss_rtk_base/run.sh` — entrypoint (bashio, MQTT credentials from Supervisor).
- `gnss_rtk_base/main.py` — application logic (str2str, MQTT, survey-in, PPP campaign).
- `gnss_rtk_base/drivers/` — per-receiver drivers (`base.py` contract,
  `unicore.py`, `ublox.py`, `__init__.py` registry); see "Multi-receiver
  support" above for how to add a new one.
- `gnss_rtk_base/nmea.py` — GGA/GST/GSV/GSA parsing (standard NMEA-0183, not receiver-specific).
- `gnss_rtk_base/ppp.py` — PPP-static processing (port of `ppp_process.py`).
- `gnss_rtk_base/state.py` — in-memory shared state (satellites/fix) between the NMEA monitor and the web server.
- `gnss_rtk_base/webui.py` — minimal HTTP server (stdlib) for the skyplot panel via Ingress.
- `gnss_rtk_base/www/index.html` — skyplot page (canvas, no external dependencies).
- `gnss_rtk_base/mqtt_discovery.py` — helper for MQTT Discovery entities.
- `gnss_rtk_base/caster.py` — optional local mini NTRIP Caster (`caster_enabled`).
- `gnss_rtk_base/position_backup.py` — on-disk backup of the computed position, with provenance.
- `gnss_rtk_base/requirements-dev.txt` — extra dependencies for tests (`pytest`).
- `gnss_rtk_base/pytest.ini` — pytest configuration (`testpaths = tests`).
- `gnss_rtk_base/tests/` — automated test suite, runnable outside
  Home Assistant (see the "Automated tests" section above).
