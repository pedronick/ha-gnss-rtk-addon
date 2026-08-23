# Changelog

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
