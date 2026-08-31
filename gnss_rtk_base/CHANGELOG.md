# Changelog

## 0.2.14

- Diagnostics: `watchdog_str2str()` now logs `str2str`'s exit code when
  it dies, e.g. "terminated unexpectedly (exit code -11), restarting..."
  (Popen convention: >=0 is the process's own exit status, negative is
  -signal - -11 is SIGSEGV, -9 SIGKILL, -6 SIGABRT). Added while
  diagnosing a real crash-loop on a user's Home Assistant instance where
  0.2.13's new `[str2str] ...` stderr logging showed "stream server
  start" right before every restart but no further error text - meaning
  str2str isn't failing to open the port with a printed error, it's
  dying (likely a signal), which this makes visible for the first time.

## 0.2.13

- Fix: `monitor_str2str_status()` silently dropped every `str2str`
  stderr line that wasn't the periodic status line - including fatal
  startup errors (e.g. a busy/wrong serial port). Found while diagnosing
  a real crash-loop on a user's Home Assistant instance where the only
  visible message was the watchdog's generic "str2str terminated
  unexpectedly, restarting...", repeated forever with no clue why.
  Non-matching lines are now printed as `[str2str] ...` so the real
  reason shows up in the add-on log. Verified locally against a real
  `str2str` given a nonexistent port (prints "stream server start" /
  "stream server start error" on stderr, exactly what was being lost).

## 0.2.12

- Added `reset(port, baud)` to the driver contract (`drivers/base.py`),
  called by `configure_receiver()` before `configure_rtcm()`/
  `configure_nmea()`. For Unicore it sends `unlogall`, clearing any
  message previously enabled outside of what this add-on manages (e.g.
  during a manual test) - those "log" commands are purely additive and
  were never cleared before. Verified on real hardware: enabled an
  unrelated message manually, confirmed it kept streaming after a normal
  `configure_rtcm`/`configure_nmea`, then confirmed `reset()` makes it
  disappear while GGA/GST/GSV/GSA/RTCM keep working normally.
  Deliberately does not touch base/rover mode, so a receiver already
  configured as a fixed base isn't reverted to rover on every add-on
  restart. The u-blox driver's `reset()` is a documented no-op (its
  `configure_rtcm`/`configure_nmea` already overwrite specific message
  rates by ID, nothing accumulates the way Unicore's "log" list does).

## 0.2.11

- Fix (PPP campaign, critical): the IGS product download had never
  actually worked, for three independent reasons, only found by testing
  a real (short) PPP campaign against actual hardware and then probing
  the real servers with curl:
  1. The primary mirror, `files.igs.org`, no longer serves IGS products
     at all - its own `readme.txt` says so and points elsewhere.
  2. The fallback mirror, CDDIS, requires NASA Earthdata credentials
     that the add-on has no way to provide.
  3. Even with a working mirror, the requested filenames were wrong:
     FIN orbit products are only published at 15M sampling (not 05M),
     and RAP clock products are only published at 05M sampling (not
     30S) - verified against a real directory listing.
  Switched the primary mirror to BKG (Bundesamt für Kartographie und
  Geodäsie, Germany), public and unauthenticated, and fixed the
  filenames - verified with a real end-to-end download (SP3 + CLK +
  ANTEX) and a real `rnx2rtkp` run.

## 0.2.10

- Fix (PPP campaign): `run_ppp_campaign()`'s temp workdir was hardcoded
  to `tempfile.TemporaryDirectory(dir="/data")` instead of being derived
  from `RAW_LOG_DIR`, breaking outside a real container where `/data`
  doesn't exist (found while testing the campaign against real
  hardware/a local test harness).
- Fix (PPP campaign): `fetch_precise_products()` only ever requested IGS
  "final" (FIN) orbit/clock products, which are published ~11-18 days
  after the fact - since the campaign processes the raw log right after
  the logging window ends, FIN products for that date are essentially
  never available yet, so the automatic campaign would fail every time
  by default. It now falls back to "rapid" (RAP, ~17-41h latency) if FIN
  isn't available, matching what the standalone `ppp_process.py` already
  supports manually via `--product RAP`.

## 0.2.9

- Fix (Unicore driver): the NMEA log commands sent by `configure_nmea()`
  ("log gga/gst/gsv/gsa ontime N") were rejected by a real UM982
  ("PARSING FAILD GRAMMAR ERROR"); the talker prefix is required
  ("gpgga"/"gpgst"/"gpgsv"/"gpgsa"), even though the module still outputs
  multi-constellation $GNGGA/... sentences. Verified against real
  hardware.
- Added the RTCM ephemeris messages (1019/1020/1042/1046) to
  `configure_rtcm()`, so a rover can reach a fix faster after a cold
  start instead of waiting to decode ephemeris from its own signal.
- Fix: when `rtcm_port == nmea_port` (the most common single-cable
  setup, and the default in the example configuration), `str2str` and
  the add-on's own NMEA monitor/survey-in were both opening the same
  physical serial port at the same time, causing
  `binary_sensor.device_connected` to flap ON/OFF every ~15-20s with
  corrupted/lost NMEA - reproduced and verified on real hardware. NMEA
  reading now goes through the same internal relay used by the local
  NTRIP caster instead of a second direct connection to the port.

## 0.2.8

- Added this changelog so the Home Assistant Add-on Store can show
  release notes when an update is available (Supervisor reads
  `CHANGELOG.md` from the add-on folder).

## 0.2.7

- Fix: the Ingress web panel (skyplot) is now started before the MQTT
  connect retry loop too, so it stays reachable even before the MQTT
  broker is available.

## 0.2.6

- Fix: the Ingress web panel was unreachable (502 Bad Gateway) while the
  add-on was still waiting for the GNSS receiver's USB port to appear.
  The web server now starts right after MQTT connects, before that
  blocking wait.

## 0.2.5

- Translated the whole add-on (code comments, log messages, MQTT entity
  names, documentation) from Italian to English.

## 0.2.4

- No functional changes (repository cleanup).

## 0.2.3

- Fix: repository URL normalized to the canonical lowercase GitHub
  username, required for Home Assistant Supervisor to detect updates
  correctly.

## 0.2.2

- No functional changes (repository cleanup).

## 0.2.1

- No functional changes (verification of the automatic version-bump
  mechanism).

## 0.2.0

- Initial release: reads a Unicore UM982/UM980 or u-blox ZED-F9P/M8P GNSS
  receiver over serial, sends RTCM corrections as an NTRIP server to one
  or more casters via RTKLIB `str2str`, exposes fix status, satellites,
  accuracy, survey-in, PPP campaign and manual position via MQTT
  Discovery, includes a real-time skyplot panel via Ingress and an
  optional local NTRIP caster for rovers.
- Fix: added `init: false` to `config.yaml`, required because the base
  image already uses s6-overlay as PID 1.
