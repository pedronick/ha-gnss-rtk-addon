# Real hardware verification protocol

Checklist to follow the first time you connect a physical receiver
(Unicore UM982 or u-blox ZED-F9P/M8P) to this add-on. Note down what
worked and what didn't: most of the protocol-specific code (`drivers/`,
`caster.py`, `str2str`'s file rotation) has only been verified with
software simulations, never with a real module — see
"Things to verify" in the add-on's `README.md`.

## 1. Receiver driver

Start the add-on with `receiver_type` set to your module and watch the
logs (the add-on's "Log" tab).

**If using Unicore (`unicore_um98x`)**: every command prints `>>` followed
by the module's raw response (`<<`). Visually check there are no errors
(the exact format of the OK/error response depends on the firmware — it
isn't parsed automatically).

**If using u-blox (`ublox_zedf9p`)**: as of this version, every command
explicitly logs `-> ACK`, `-> NAK (command rejected by the module!)`, or
`-> no response within timeout`. Any log other than `ACK` is a
reliable sign of a problem:
- **NAK**: the module understood the command but rejected it — check
  that the RTCM/NMEA message ID is correct for your firmware
  (`drivers/ublox.py`, `RTCM_MSG_IDS`/`NMEA_MSG_IDS` dictionaries).
- **No response**: likely a port/baud rate issue, not a command content
  issue (the module isn't even receiving the frame).

What to concretely verify:
- [ ] All `configure_rtcm`/`configure_nmea` commands get an ACK
      (u-blox) or an error-free response (Unicore).
- [ ] After configuration, the module actually emits NMEA GGA at
      1Hz on the indicated port (verifiable by opening the port with a
      serial terminal, e.g. `screen /dev/ttyUSB0 115200`).
- [ ] The module emits RTCM3 (unreadable binary data) on the RTCM port.

## 2. `str2str` (RTKLIB)

- [ ] In the add-on logs, the `str2str` command printed at startup
      starts without immediate errors.
- [ ] If you configured an external caster (`ntrip_casters`), check on
      the caster side (e.g. RTK2go dashboard) that the mountpoint shows
      as online and receiving data.
- [ ] Verify that files appear in `/data/raw_logs/gnssbase_*.rtcm3`
      (accessible via SSH terminal on the host, or the Home Assistant
      "File editor"/"Studio Code Server" add-on) and that a **new file
      appears every hour** (verifying the `%Y%m%d%h`-based rotation,
      never tested with a real RTKLIB build).
- [ ] If a file grows but stays at 0 bytes for minutes, the module is
      probably not providing valid RTCM on the configured port.

## 3. Local caster (if `caster_enabled: true`)

- [ ] From a second device on the same network, try connecting with a
      **real NTRIP client** (not just a manual test):
      - Mobile app: SW Maps, Emlid Flow, or similar (custom "NTRIP
        client" mode, with the add-on's host/port/mountpoint/credentials).
      - From a PC: `str2str -in ntrip://user:pass@<addon-ip>:2101/<mountpoint> -out file://test.rtcm3`
        and verify that `test.rtcm3` grows over time.
- [ ] Check `sensor.local_caster_connected_rovers` in Home Assistant:
      it should increment when a client connects.
- [ ] If the client connects but receives no data, the problem is almost
      certainly in the handshake (see the "Ntrip-Version: Ntrip/2.0" note
      in the README) — capture traffic with `tcpdump -i any port 2101 -w capture.pcap`
      and inspect with Wireshark what the client sends before receiving
      a response.

## 4. USB resilience

- [ ] Physically unplug the receiver's USB cable while the add-on is
      running: `binary_sensor.device_connected` should switch to OFF
      within ~15-20 seconds.
- [ ] Reconnect the cable: it should go back to ON within a few seconds,
      without needing to restart the add-on.
- [ ] Restart the host/VM with the receiver already connected: the
      add-on should start correctly even if the USB isn't enumerated yet
      in the first moments of boot.

## 5. Survey-in and PPP campaign

- [ ] `button.start_survey_in`: after `survey_in_duration_sec`, the
      module should end up in fixed-base mode with plausible coordinates
      (visually comparable to the real position, e.g. from Google Maps,
      expected tolerance: a few meters).
- [ ] `button.start_ppp_campaign`: once the campaign completes, compare
      the obtained position with one computed independently by
      `ppp_process.py` (top-level folder) or by the CSRS-PPP online
      service over the same log interval — they should match within a
      few centimeters.

## If something doesn't look right

Tell me exactly: which checklist item fails, the `receiver_type` in use,
and the relevant add-on log output (in particular the
`[unicore]`/`[ublox]`/`[main]`/`[caster]` lines). From there I can figure
out whether the problem is in the command syntax, the parsing, or
elsewhere.
