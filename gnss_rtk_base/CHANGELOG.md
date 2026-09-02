# Changelog

## 0.2.21

- Feature: new `button.reprocess_existing_logs` runs the PPP-static
  pipeline (RINEX conversion → IGS products → `rnx2rtkp`) directly over
  whatever raw log files are still retained on disk (up to
  `raw_log_retention_hours` back), with no new logging phase. Recovers a
  campaign whose intermediate files were lost some way other than the
  computation failure `retry_ppp_computation` (0.2.20) already covers -
  the underlying raw logs are independently retained regardless of
  whether a campaign is running, so nothing is actually lost as long as
  they're still within the retention window. Shares the same
  `sensor.ppp_campaign` state machine and is mutually exclusive with a
  normal campaign/retry.
- Refactored `run_ppp_campaign()`'s raw-to-RINEX conversion and IGS
  product retry-wait into `_convert_raw_window_to_rinex()` and
  `_wait_for_products()`, now shared with `reprocess_existing_logs()`
  instead of being campaign-specific.

## 0.2.20

- Feature: if a PPP campaign fails at the very last step (`rnx2rtkp`,
  e.g. the invalid config values fixed in 0.2.19) after the IGS products
  were already successfully downloaded, the already-downloaded products
  and converted RINEX files are now kept instead of being deleted. New
  `button.retry_ppp_computation` retries just that computation step with
  the same data - no re-logging (potentially hours), no re-downloading.
  Superseded (becomes a no-op) as soon as a new campaign starts, since
  starting one already discards the old campaign's working files.
  Extracted the shared "apply position, save backup, maybe schedule
  refinement" logic (introduced in 0.2.18) into `_compute_and_finish_ppp()`,
  used by both the normal campaign flow and this retry path.

## 0.2.19

- **Critical fix**: `PPP_CONF_TEMPLATE` used `pos1-frequency=l1+l2` and
  `pos1-ionoopt=iflc`, neither of which are valid RTKLIB option values -
  `rnx2rtkp` rejected both with "invalid option value" and silently fell
  back to defaults that produced an all-`Q=0` `result.pos` (no fix at
  all), so every PPP campaign that got as far as actually running
  `rnx2rtkp` still failed with `No valid epoch found in result.pos`.
  Found from a real campaign on a user's Home Assistant instance that
  finally got IGS products after ~26 hourly retries (see 0.2.17), only to
  fail at this last step. Verified against RTKLIB's real enum
  definitions (`src/options.c`'s `FRQOPT`/`IONOPT`): the correct values
  are `l1+2` (dual-frequency) and `dual-freq` (ionosphere-free
  combination via dual-frequency observations) - confirmed with a real
  `rnx2rtkp` run producing no more "invalid option value" warnings.

## 0.2.18

- Feature: once a PPP campaign completes using "rapid" (RAP) IGS
  products rather than the more precise "final" (FIN) ones - the normal
  case, since FIN takes ~11-18 days - the add-on now checks once a day in
  the background for FIN becoming available, instead of never trying
  again. This doesn't block a new campaign from being started in the
  meantime: it's a separate task, tracked by the new
  `sensor.ppp_refinement_status` (idle / waiting_for_final / available /
  error), superseded automatically if a new campaign starts. If/when FIN
  shows up, the refined position is published to the same manual
  position fields (`number.manual_latitude`/etc.) used everywhere else -
  not applied to the receiver automatically, consistent with this add-on
  never changing the base's live position without an explicit
  `button.apply_manual_position`. The check survives an add-on restart
  (resumed from the persisted RINEX files on disk), since the whole point
  is to survive until the next campaign starts, not until the next
  restart.

## 0.2.17

- Feature: the PPP campaign no longer fails outright when IGS products
  aren't published yet for such a recent date (a near-certainty for a
  same-day campaign: "final" products take ~11-18 days, "rapid" ones
  ~17-41 hours). It now converts the raw log to RINEX right away (no
  network needed for that part), and if the product download isn't ready,
  moves to a new `waiting_for_products` state and retries automatically
  every hour, bounded by `raw_log_retention_hours` (retrying past that
  point is pointless: the source raw log will already have been deleted
  by the periodic cleanup by then). `sensor.ppp_campaign_time_remaining`
  keeps counting down during the wait, and `button.cancel_ppp_campaign`
  still works. See the README's "waiting_for_products" section for what
  this means for `raw_log_retention_hours` sizing.

## 0.2.16

- Fix (PPP campaign): `try_download()` accepted any HTTP 200 response
  over 1000 bytes as a successful download, without checking it was
  actually the expected file. A mirror requiring authentication (e.g.
  CDDIS without NASA Earthdata credentials) can serve an HTML
  login/error page with a 200 status, long enough to pass that check -
  found from a real PPP campaign run on a user's Home Assistant
  instance, which failed deep inside gzip decompression with a
  confusing `Not a gzipped file (b'<!')` instead of a clear "download
  failed". Now rejects responses that look like HTML (leading `<` or an
  `html` Content-Type) before accepting them, so a real failure now
  reports plainly ("No IGS products available for <date> (tried: FIN,
  RAP)") instead of a cryptic downstream error. Verified against the
  real network: a very recent date now fails cleanly (as expected: even
  "rapid" IGS products aren't published yet for it), and an older date
  still downloads correctly.

## 0.2.15

- **Critical fix**: `str2str` crashed with a real SIGSEGV as soon as any
  TCP client output stream (`ntrips://` external NTRIP casters, and this
  add-on's own internal relay) received data, on this add-on's Alpine
  base image. Root-caused with a real build of this image and a gdb
  backtrace: RTKLIB's `gentcp()` (`src/stream.c`) resolves the target
  address with the legacy `gethostbyname()`, which corrupts memory on
  musl even for a plain numeric IP like `127.0.0.1`. This means **every
  external NTRIP caster configured in `ntrip_casters` was silently
  broken** on real Home Assistant installations - found while diagnosing
  a real crash-loop reported by a user, reproduced locally by building
  this exact image and replaying real captured RTCM3/NMEA data through
  it. Fixed at the source with `patches/0001-rtklib-gentcp-getaddrinfo.patch`,
  applied to RTKLIB during the Docker build: replaces `gethostbyname()`
  with `getaddrinfo()`, the modern POSIX-standard, thread-safe
  replacement, unaffected on both glibc and musl - verified by
  rebuilding the image and confirming the crash no longer reproduces,
  for both the external-caster and internal-relay cases.
- The internal relay (used by the local NTRIP caster and, when
  `rtcm_port == nmea_port`, by NMEA monitoring/survey-in) now has
  `str2str` act as the TCP server (`tcpsvr://`) instead of the client
  (`tcpcli://`), with `caster.py` connecting to it instead of the other
  way around: `gentcp()`'s server-role code path never calls
  `gethostbyname()`, so this was already immune to the bug above even
  before the RTKLIB patch - kept as defense in depth. Fixed a related bug
  introduced by this change: an abrupt disconnection (e.g. a `str2str`
  restart) raised `ConnectionResetError` from `recv()`, which wasn't
  caught, silently killing the relay's reconnect loop.

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
