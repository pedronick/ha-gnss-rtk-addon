# Changelog

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
