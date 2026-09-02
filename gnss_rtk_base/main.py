#!/usr/bin/env python3
"""
RTK Base Station add-on: reads a GNSS RTK receiver over serial (driver
selectable via receiver_type, see drivers/), pushes RTCM corrections as
an NTRIP server (RTKLIB str2str) to one or more casters, and exposes
status/controls in Home Assistant via MQTT Discovery.
"""

import datetime as dt
import glob
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import serial

import caster
import drivers
import mqtt_discovery as disc
import nmea
import ppp
import position_backup
from state import SharedState
from webui import start_webserver

OPTIONS_PATH = "/data/options.json"
RAW_LOG_DIR = "/data/raw_logs"
BASE = "gnssbase"
WEBUI_PORT = 8099
SERIAL_RETRY_INTERVAL_S = 5
SERIAL_SILENCE_TIMEOUT_S = 15
MQTT_RETRY_INTERVAL_S = 10
RELAY_READ_TIMEOUT_S = 1  # socket timeout for _relay_line_reader(), module-level so tests can shorten it
PPP_PRODUCT_RETRY_INTERVAL_S = 3600  # how often to retry downloading IGS products while waiting_for_products
PPP_REFINEMENT_CHECK_INTERVAL_S = 86400  # how often to check for FIN products once a RAP-based fix is applied

# str2str status line format (verified with a real binary):
# "2024/01/15 12:34:56 [CC---]        425 B     699 bps (1) send error (111) "
STR2STR_STATUS_RE = re.compile(r"\[(?P<statuses>[^\]]*)\]\s+\d+\s+B\s+(?P<bps>-?\d+)\s+bps\s*(?P<msg>.*)$")
STR2STR_STATUS_LABELS = {"E": "Error", "-": "Closed", "W": "Waiting", "C": "Connected"}


def load_options():
    with open(OPTIONS_PATH) as f:
        return json.load(f)


def wait_for_serial_port(path, label):
    """Blocks until the serial device exists. Useful both at startup (the
    USB might not be enumerated yet) and after a disconnection."""
    first = True
    while not os.path.exists(path):
        if first:
            print(f"[main] {label}: port {path} not found, waiting for it to appear...", flush=True)
            first = False
        time.sleep(SERIAL_RETRY_INTERVAL_S)


class App:
    def __init__(self, opts):
        self.rtcm_port = opts["rtcm_port"]
        self.nmea_port = opts["nmea_port"]
        self.baud = opts["baudrate"]
        self.receiver_type = opts.get("receiver_type", "unicore_um98x")
        self.driver = drivers.get_driver(self.receiver_type)
        disc.DEVICE["model"] = getattr(self.driver, "NAME", self.receiver_type)
        self.survey_duration = opts.get("survey_in_duration_sec", 300)
        self.ntrip_casters = opts.get("ntrip_casters", [])
        self.ppp_duration_hours = opts.get("ppp_duration_hours", 6)
        self.raw_log_retention_hours = opts.get("raw_log_retention_hours", 72)
        self.caster_enabled = opts.get("caster_enabled", False)
        self.caster_mountpoint = opts.get("caster_mountpoint", "GNSSBASE")
        self.caster_user = opts.get("caster_user", "")
        self.caster_password = opts.get("caster_password", "")
        self.caster_max_clients = opts.get("caster_max_clients", 10)
        # Always created (cheap, no listening socket yet): needed both for
        # the local NTRIP caster (caster_enabled) and, when rtcm_port ==
        # nmea_port, as the way monitor_nmea()/run_survey_in() read NMEA
        # without opening the physical serial port a second time (see
        # needs_internal_relay()).
        self.broadcaster = caster.Broadcaster()
        self._last_fix_publish = 0

        self.str2str_proc = None
        self.survey_running = False
        self.ppp_running = False
        self.survey_cancel_event = None
        self.ppp_cancel_event = None
        # Bumped each time a new PPP campaign starts: invalidates (and
        # stops) any run_ppp_refinement() background thread left over
        # from a previous campaign, since only one refinement should be
        # active at a time.
        self.ppp_refinement_id = 0
        # Set by _compute_and_finish_ppp() when rnx2rtkp/parsing fails
        # after IGS products were already downloaded: holds what
        # retry_ppp_computation() needs to retry just that step. None
        # when there's nothing retryable.
        self.ppp_retry_state = None
        self.manual_lat = None
        self.manual_lon = None
        self.manual_height = None

        Path(RAW_LOG_DIR).mkdir(parents=True, exist_ok=True)
        self.state = SharedState()

        self.mqtt = mqtt.Client()
        user = os.environ.get("MQTT_USER")
        password = os.environ.get("MQTT_PASSWORD")
        if user:
            self.mqtt.username_pw_set(user, password)
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_message = self.on_message

    # ---------------------------------------------------------------- setup

    def set_device_connected(self, connected, reason=""):
        if connected == getattr(self, "_last_connected_state", None):
            if connected:
                self.mqtt.publish(f"{BASE}/last_seen/state",
                                   dt.datetime.now(dt.timezone.utc).isoformat(), retain=True)
            return
        self._last_connected_state = connected
        self.mqtt.publish(f"{BASE}/device_connected/state", "ON" if connected else "OFF", retain=True)
        if connected:
            self.mqtt.publish(f"{BASE}/last_seen/state",
                               dt.datetime.now(dt.timezone.utc).isoformat(), retain=True)
        status = "connected" if connected else f"NOT connected ({reason})" if reason else "NOT connected"
        print(f"[main] device status: {status}", flush=True)

    def configure_receiver(self):
        """Resets any leftover message/log configuration (driver.reset(),
        see drivers/unicore.py for why this matters on that driver) and
        configures RTCM and NMEA on the module, waiting for the serial
        ports to exist (useful if the USB is not yet enumerated when the
        add-on starts) and retrying on error instead of crashing the
        whole add-on."""
        for port, label in ((self.rtcm_port, "RTCM"), (self.nmea_port, "NMEA")):
            wait_for_serial_port(port, label)

        while True:
            try:
                self.driver.reset(self.rtcm_port, self.baud)
                self.driver.configure_rtcm(self.rtcm_port, self.baud)
                if self.nmea_port != self.rtcm_port:
                    time.sleep(0.5)
                    wait_for_serial_port(self.nmea_port, "NMEA")
                self.driver.configure_nmea(self.nmea_port, self.baud)
                return
            except (serial.SerialException, OSError) as e:
                print(f"[main] error configuring the module ({e}), retrying in "
                      f"{SERIAL_RETRY_INTERVAL_S}s...", flush=True)
                time.sleep(SERIAL_RETRY_INTERVAL_S)

    def active_casters(self):
        return [c for c in self.ntrip_casters if c.get("host")]

    def needs_internal_relay(self):
        """Whether str2str needs an extra -out tcpsvr://:... feeding
        caster.py's internal relay (see build_str2str_cmd/run()). True if the
        local NTRIP caster is enabled (rovers need the byte stream), or if
        rtcm_port == nmea_port: in that case str2str is already the sole
        process reading the physical serial port continuously, so
        monitor_nmea()/run_survey_in() must get their NMEA lines from this
        relay instead of opening the same device a second time - verified on
        real hardware that a second direct serial.Serial() there races with
        str2str for bytes on the same character device, flapping
        device_connected ON/OFF every ~15-20s with corrupted/lost NMEA."""
        return self.caster_enabled or self.nmea_port == self.rtcm_port

    def output_stream_plan(self):
        """Labels for the -out streams passed to str2str, in the same
        order in which they are added by build_str2str_cmd(). Used to
        interpret the 5-character status string that str2str prints
        periodically on stderr: index 0 = serial input, index i = the
        i-th -out in this same order (verified by reading
        strsvrstat()/str2str.c in RTKLIB and with a compiled binary)."""
        labels = [f"{c['host']}:{c['port']}/{c.get('mountpoint', '')}" for c in self.active_casters()]
        labels.append("raw log")
        if self.needs_internal_relay():
            labels.append("local caster" if self.caster_enabled else "internal relay (NMEA)")
        return labels

    def validate_stream_budget(self):
        """RTKLIB limits str2str to MAXSTR=5 total streams (1 input + 4
        outputs, verified in the app/consapp/str2str/str2str.c source):
        beyond the fourth -out, extra arguments are silently and
        unpredictably ignored, not with a clear error. Better to fail
        loudly here than discover it at runtime."""
        needed = len(self.output_stream_plan())
        if needed > 4:
            relay_reason = ""
            if self.needs_internal_relay():
                relay_reason = (" + 1 local caster" if self.caster_enabled
                                 else " + 1 internal relay (rtcm_port == nmea_port)")
            raise ValueError(
                f"Too many outputs configured for str2str: {needed} "
                f"({len(self.active_casters())} NTRIP caster(s) + 1 raw log"
                f"{relay_reason}), "
                f"the maximum supported by RTKLIB is 4. Reduce the number "
                f"of entries in ntrip_casters, disable caster_enabled, or use "
                f"a separate nmea_port."
            )

    def build_str2str_cmd(self):
        """A single str2str instance reads the serial port only once and
        fans it out to multiple -out targets (one per caster + one for
        the continuous raw log). Reading the same serial port from
        multiple independent processes would corrupt the stream: str2str
        natively supports multiple -out targets exactly for this use case
        (max 4, see validate_stream_budget)."""
        # str2str uses serial://<device_name>, without "/dev/": it
        # prepends that itself internally (openserial() in src/stream.c
        # does sprintf(dev, "/dev/%s", port)). Passing "/dev/ttyUSB0"
        # would produce "/dev//dev/ttyUSB0", which doesn't exist: str2str
        # would never start. Verified by actually compiling and running
        # str2str with a full path (fails) and with the bare name (works).
        str2str_port = self.rtcm_port.removeprefix("/dev/")
        cmd = ["str2str", "-in", f"serial://{str2str_port}:{self.baud}"]
        for caster_cfg in self.active_casters():
            # RTKLIB syntax for an NTRIP server output: "ntrips://[:passwd@]addr[:port]/mntpnt".
            # There's no user field: str2str, in the server/encoder role,
            # only sends the mountpoint password (NTRIP1 protocol "SOURCE
            # <password> <mountpoint>" — verified in RTKLIB's
            # src/stream.c/reqntrip_s, which never uses the username for
            # this role).
            out = (f"ntrips://:{caster_cfg.get('password', '')}"
                   f"@{caster_cfg['host']}:{caster_cfg['port']}/{caster_cfg.get('mountpoint', '')}")
            cmd += ["-out", out]
        # Continuous raw log, with automatic hourly rotation (str2str
        # recognizes the %Y%m%d%h tags in the path and creates a new file
        # every hour).
        cmd += ["-out", f"file://{RAW_LOG_DIR}/gnssbase_%Y%m%d%h.rtcm3"]
        if self.needs_internal_relay():
            # str2str listens as a TCP server and caster.py's
            # run_relay_receiver() connects to it as a client (backwards
            # from what you'd expect - see that function's docstring:
            # RTKLIB's tcpcli client role crashes with a real SIGSEGV on
            # Alpine/musl, tcpsvr doesn't). Consumers of the relayed
            # bytes: connected rovers (if caster_enabled) and/or our own
            # NMEA monitor/survey-in (if rtcm_port == nmea_port, see
            # needs_internal_relay()).
            cmd += ["-out", f"tcpsvr://:{caster.INTERNAL_RELAY_PORT}"]
        return cmd

    def start_str2str(self):
        cmd = self.build_str2str_cmd()
        print("[main] starting str2str:", " ".join(cmd), flush=True)
        self.str2str_proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self.monitor_str2str_status, args=(self.str2str_proc,), daemon=True).start()

    def watchdog_str2str(self):
        while True:
            time.sleep(10)
            if self.str2str_proc is not None:
                code = self.str2str_proc.poll()
                if code is not None:
                    # Popen.returncode: >=0 is the process's own exit code,
                    # negative is -signal (e.g. -11 = SIGSEGV, -9 = SIGKILL,
                    # -6 = SIGABRT) - distinguishes "str2str exited on its
                    # own" from "something killed it", which the previous
                    # message alone couldn't (found missing while
                    # diagnosing a real crash-loop with no other clue).
                    print(f"[main] str2str terminated unexpectedly (exit code {code}), "
                          f"restarting...", flush=True)
                    self.str2str_proc = subprocess.Popen(
                        self.str2str_proc.args, stderr=subprocess.PIPE, text=True, bufsize=1)
                    threading.Thread(target=self.monitor_str2str_status, args=(self.str2str_proc,), daemon=True).start()

    def monitor_str2str_status(self, proc):
        """Reads str2str's stderr (it prints a status line every 5s by
        default: '<date/time> [<5-char status>] <byte> B <bps> bps
        <per-stream messages>') and translates it into per-output MQTT
        entities. Format verified by actually compiling and running
        str2str, not just deduced from the help text."""
        labels = self.output_stream_plan()
        for line in proc.stderr:
            if proc is not self.str2str_proc:
                return  # restarted by the watchdog: this thread is stale
            m = STR2STR_STATUS_RE.search(line)
            if not m:
                # Not a periodic status line - most likely a fatal
                # startup error (e.g. "openserial: device busy", wrong
                # port). Previously silently dropped here, which meant
                # str2str's actual failure reason never appeared
                # anywhere, only "terminated unexpectedly, restarting..."
                # from the watchdog - found while diagnosing a real
                # crash-loop where the cause was invisible in the logs.
                stripped = line.rstrip("\n")
                if stripped:
                    print(f"[str2str] {stripped}", flush=True)
                continue
            statuses = m.group("statuses")
            self.mqtt.publish(f"{BASE}/rtcm_bps/state", int(m.group("bps")), retain=True)
            for i, label in enumerate(labels):
                char = statuses[i + 1] if i + 1 < len(statuses) else "-"
                status_label = STR2STR_STATUS_LABELS.get(char, f"? ({char})")
                self.mqtt.publish(f"{BASE}/output_{i}_status/state", status_label, retain=True)
            msg = m.group("msg").strip()
            if msg:
                self.mqtt.publish(f"{BASE}/str2str_diagnostics/state", msg, retain=True)

    def publish_caster_clients(self):
        while True:
            self.mqtt.publish(f"{BASE}/caster_clients/state", self.broadcaster.num_clients(), retain=True)
            time.sleep(5)

    def cleanup_raw_logs(self):
        """Periodically deletes raw log files older than the configured
        retention, to avoid filling up the disk (logging is always on so
        a PPP campaign can be started at any time over a recent
        window)."""
        while True:
            cutoff = time.time() - self.raw_log_retention_hours * 3600
            for path in glob.glob(f"{RAW_LOG_DIR}/gnssbase_*.rtcm3"):
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass
            time.sleep(3600)

    # ------------------------------------------------------------ discovery

    def publish_discovery(self):
        d = disc
        d.publish_discovery(self.mqtt, "sensor", "fix_status", d.sensor_config(
            "fix_status", "Fix Status", f"{BASE}/fix_status/state", icon="mdi:crosshairs-gps"))
        d.publish_discovery(self.mqtt, "sensor", "satellites", d.sensor_config(
            "satellites", "Satellites in use", f"{BASE}/satellites/state", unit="satellites"))
        d.publish_discovery(self.mqtt, "sensor", "accuracy", d.sensor_config(
            "accuracy", "Estimated accuracy", f"{BASE}/accuracy/state", unit="m"))
        d.publish_discovery(self.mqtt, "sensor", "survey_in_status", d.sensor_config(
            "survey_in_status", "Survey-In", f"{BASE}/survey_in/state"))
        d.publish_discovery(self.mqtt, "button", "survey_in_start", d.button_config(
            "survey_in_start", "Start Survey-In", f"{BASE}/survey_in/set", icon="mdi:crosshairs"))
        d.publish_discovery(self.mqtt, "button", "survey_in_cancel", d.button_config(
            "survey_in_cancel", "Cancel Survey-In", f"{BASE}/survey_in_cancel/set", icon="mdi:cancel"))
        d.publish_discovery(self.mqtt, "sensor", "survey_in_remaining", d.sensor_config(
            "survey_in_remaining", "Survey-In: time remaining", f"{BASE}/survey_in_remaining/state",
            unit="s", icon="mdi:timer-sand", device_class="duration"))
        d.publish_discovery(self.mqtt, "number", "manual_lat", d.number_config(
            "manual_lat", "Manual latitude", f"{BASE}/manual_lat/state",
            f"{BASE}/manual_lat/set", -90, 90, 0.0000001, "°"))
        d.publish_discovery(self.mqtt, "number", "manual_lon", d.number_config(
            "manual_lon", "Manual longitude", f"{BASE}/manual_lon/state",
            f"{BASE}/manual_lon/set", -180, 180, 0.0000001, "°"))
        d.publish_discovery(self.mqtt, "number", "manual_height", d.number_config(
            "manual_height", "Manual height", f"{BASE}/manual_height/state",
            f"{BASE}/manual_height/set", -500, 9000, 0.001, "m"))
        d.publish_discovery(self.mqtt, "button", "apply_manual_position", d.button_config(
            "apply_manual_position", "Apply manual position",
            f"{BASE}/apply_manual_position/set", icon="mdi:map-marker-check"))
        d.publish_discovery(self.mqtt, "sensor", "ppp_status", d.sensor_config(
            "ppp_status", "PPP campaign", f"{BASE}/ppp_status/state", icon="mdi:satellite-variant"))
        d.publish_discovery(self.mqtt, "number", "ppp_duration_hours", d.number_config(
            "ppp_duration_hours", "PPP campaign duration", f"{BASE}/ppp_duration_hours/state",
            f"{BASE}/ppp_duration_hours/set", 1, 48, 1, "h"))
        d.publish_discovery(self.mqtt, "button", "ppp_start", d.button_config(
            "ppp_start", "Start PPP campaign", f"{BASE}/ppp_start/set", icon="mdi:satellite-uplink"))
        d.publish_discovery(self.mqtt, "button", "ppp_cancel", d.button_config(
            "ppp_cancel", "Cancel PPP campaign", f"{BASE}/ppp_cancel/set", icon="mdi:cancel"))
        d.publish_discovery(self.mqtt, "sensor", "ppp_remaining", d.sensor_config(
            "ppp_remaining", "PPP campaign: time remaining", f"{BASE}/ppp_remaining/state",
            unit="s", icon="mdi:timer-sand", device_class="duration"))
        d.publish_discovery(self.mqtt, "sensor", "ppp_refinement_status", d.sensor_config(
            "ppp_refinement_status", "PPP refinement (final products)",
            f"{BASE}/ppp_refinement_status/state", icon="mdi:satellite-variant-outline"))
        d.publish_discovery(self.mqtt, "button", "ppp_retry_computation", d.button_config(
            "ppp_retry_computation", "Retry PPP computation (same IGS products)",
            f"{BASE}/ppp_retry_computation/set", icon="mdi:calculator-variant"))
        d.publish_discovery(self.mqtt, "button", "ppp_reprocess", d.button_config(
            "ppp_reprocess", "Reprocess existing raw logs (no new logging)",
            f"{BASE}/ppp_reprocess/set", icon="mdi:database-refresh"))
        if self.caster_enabled:
            d.publish_discovery(self.mqtt, "sensor", "caster_clients", d.sensor_config(
                "caster_clients", "Connected rovers (local caster)",
                f"{BASE}/caster_clients/state", unit="rovers", icon="mdi:radio-tower"))
        d.publish_discovery(self.mqtt, "binary_sensor", "device_connected", d.binary_sensor_config(
            "device_connected", "Device connected", f"{BASE}/device_connected/state",
            device_class="connectivity"))
        d.publish_discovery(self.mqtt, "sensor", "last_seen", d.sensor_config(
            "last_seen", "Last data received", f"{BASE}/last_seen/state",
            icon="mdi:clock-outline", device_class="timestamp"))
        d.publish_discovery(self.mqtt, "sensor", "config_error", d.sensor_config(
            "config_error", "Configuration error", f"{BASE}/config_error/state",
            icon="mdi:alert-circle-outline"))
        d.publish_discovery(self.mqtt, "sensor", "position_backup", d.sensor_config(
            "position_backup", "Last computed position", f"{BASE}/position_backup/state",
            icon="mdi:map-marker-check-outline", device_class="timestamp",
            json_attributes_topic=f"{BASE}/position_backup/attributes"))
        d.publish_discovery(self.mqtt, "sensor", "rtcm_bps", d.sensor_config(
            "rtcm_bps", "RTCM input bitrate", f"{BASE}/rtcm_bps/state",
            unit="bps", icon="mdi:speedometer"))
        d.publish_discovery(self.mqtt, "sensor", "str2str_diagnostics", d.sensor_config(
            "str2str_diagnostics", "str2str diagnostics", f"{BASE}/str2str_diagnostics/state",
            icon="mdi:text-box-outline"))
        for i, label in enumerate(self.output_stream_plan()):
            d.publish_discovery(self.mqtt, "sensor", f"output_{i}_status", d.sensor_config(
                f"output_{i}_status", f"Output {i + 1} status: {label}",
                f"{BASE}/output_{i}_status/state", icon="mdi:transmission-tower"))

    def on_connect(self, client, userdata, flags, rc):
        print("[mqtt] connected, rc=", rc, flush=True)
        self.publish_discovery()
        for topic in ("survey_in/set", "survey_in_cancel/set", "manual_lat/set", "manual_lon/set",
                      "manual_height/set", "apply_manual_position/set",
                      "ppp_duration_hours/set", "ppp_start/set", "ppp_cancel/set",
                      "ppp_retry_computation/set", "ppp_reprocess/set"):
            client.subscribe(f"{BASE}/{topic}")
        client.publish(f"{BASE}/survey_in/state", "idle", retain=True)
        client.publish(f"{BASE}/survey_in_remaining/state", 0, retain=True)
        client.publish(f"{BASE}/ppp_status/state", "idle", retain=True)
        client.publish(f"{BASE}/ppp_remaining/state", 0, retain=True)
        client.publish(f"{BASE}/ppp_duration_hours/state", self.ppp_duration_hours, retain=True)
        self.restore_position_backup()

    def restore_position_backup(self):
        """At startup, restores the last computed position in memory (if
        present on disk) and exposes it as the 'manual position' and as
        the backup sensor — useful if the container gets recreated: the
        receiver may already have its config saved, but the add-on
        itself would otherwise not remember anything about how/when it
        was computed. Doesn't send it back to the receiver: only
        data/visibility, applying it remains a deliberate action
        (button)."""
        data = position_backup.load()
        if not data:
            return
        self.manual_lat, self.manual_lon, self.manual_height = data["lat"], data["lon"], data["height"]
        self.mqtt.publish(f"{BASE}/manual_lat/state", f"{data['lat']:.8f}", retain=True)
        self.mqtt.publish(f"{BASE}/manual_lon/state", f"{data['lon']:.8f}", retain=True)
        self.mqtt.publish(f"{BASE}/manual_height/state", f"{data['height']:.3f}", retain=True)
        self.mqtt.publish(f"{BASE}/position_backup/state", data["computed_at"], retain=True)
        self.mqtt.publish(f"{BASE}/position_backup/attributes", json.dumps(data), retain=True)
        print(f"[main] backup position restored: {data['method']} @ {data['computed_at']}", flush=True)

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="replace").strip()
        if msg.topic.endswith("survey_in/set"):
            threading.Thread(target=self.run_survey_in, daemon=True).start()
        elif msg.topic.endswith("survey_in_cancel/set"):
            self.cancel_survey_in()
        elif msg.topic.endswith("manual_lat/set"):
            self.manual_lat = float(payload)
            client.publish(f"{BASE}/manual_lat/state", payload, retain=True)
        elif msg.topic.endswith("manual_lon/set"):
            self.manual_lon = float(payload)
            client.publish(f"{BASE}/manual_lon/state", payload, retain=True)
        elif msg.topic.endswith("manual_height/set"):
            self.manual_height = float(payload)
            client.publish(f"{BASE}/manual_height/state", payload, retain=True)
        elif msg.topic.endswith("apply_manual_position/set"):
            self.apply_manual_position()
        elif msg.topic.endswith("ppp_duration_hours/set"):
            self.ppp_duration_hours = float(payload)
            client.publish(f"{BASE}/ppp_duration_hours/state", payload, retain=True)
        elif msg.topic.endswith("ppp_start/set"):
            threading.Thread(target=self.run_ppp_campaign, daemon=True).start()
        elif msg.topic.endswith("ppp_cancel/set"):
            self.cancel_ppp_campaign()
        elif msg.topic.endswith("ppp_retry_computation/set"):
            threading.Thread(target=self.retry_ppp_computation, daemon=True).start()
        elif msg.topic.endswith("ppp_reprocess/set"):
            threading.Thread(target=self.reprocess_existing_logs, daemon=True).start()

    def cancel_survey_in(self):
        if self.survey_running and self.survey_cancel_event:
            print("[survey-in] cancellation request received", flush=True)
            self.survey_cancel_event.set()

    def cancel_ppp_campaign(self):
        if self.ppp_running and self.ppp_cancel_event:
            print("[ppp] cancellation request received", flush=True)
            self.ppp_cancel_event.set()

    # ------------------------------------------------------------ survey-in

    def save_position_backup(self, lat, lon, height, method, **extra):
        """Saves to /data the position just fixed, with provenance
        (method, timestamp, parameters), and publishes the same content
        as an MQTT entity (state = timestamp, attributes = the rest of
        the data). Returns that data (in particular computed_at), used by
        _compute_and_finish_ppp to name the permanent raw-log archive
        (see _archive_ppp_source_logs) for the same computation."""
        data = position_backup.save(lat, lon, height, method, self.receiver_type, **extra)
        self.mqtt.publish(f"{BASE}/position_backup/state", data["computed_at"], retain=True)
        self.mqtt.publish(f"{BASE}/position_backup/attributes", json.dumps(data), retain=True)
        return data

    def _relay_line_reader(self):
        """Subscribes as a client of the internal relay fed by str2str
        (same mechanism as the local NTRIP caster) and returns
        (read_line, close): read_line() behaves like pyserial's
        readline(timeout=1) - blocks up to ~1s, returns "" on timeout or
        if the underlying connection is gone. Used instead of a direct
        serial.Serial() whenever rtcm_port == nmea_port, since str2str is
        already the sole reader of that physical port (see
        needs_internal_relay())."""
        pub_sock, priv_sock = socket.socketpair()
        self.broadcaster.add_client(pub_sock)
        priv_sock.settimeout(RELAY_READ_TIMEOUT_S)
        buf = bytearray()

        def read_line():
            while b"\n" not in buf:
                try:
                    chunk = priv_sock.recv(4096)
                except socket.timeout:
                    return ""
                except OSError:
                    return ""
                if not chunk:
                    return ""
                buf.extend(chunk)
            idx = buf.index(b"\n")
            line = bytes(buf[:idx + 1])
            del buf[:idx + 1]
            return line.decode(errors="replace")

        def close():
            try:
                priv_sock.close()
            except OSError:
                pass

        return read_line, close

    def _process_nmea_line(self, line):
        gga = nmea.parse_gga(line)
        if gga:
            self.state.update_gga(gga)
            now = time.time()
            if now - self._last_fix_publish > 1:
                self.mqtt.publish(f"{BASE}/fix_status/state", nmea.fix_label(gga["quality"]), retain=True)
                self.mqtt.publish(f"{BASE}/satellites/state", gga["num_sv"], retain=True)
                self._last_fix_publish = now
            return
        gst = nmea.parse_gst(line)
        if gst:
            self.state.update_gst(gst["accuracy_m"])
            self.mqtt.publish(f"{BASE}/accuracy/state", gst["accuracy_m"], retain=True)
            return
        gsv = nmea.parse_gsv(line)
        if gsv:
            self.state.update_gsv(gsv)
            return
        gsa = nmea.parse_gsa(line)
        if gsa:
            self.state.update_gsa(gsa)

    def run_survey_in(self):
        """Averages standalone (single-point) positions for a short time.

        WARNING: this is a "quick" consumer-style survey-in (typical
        meter/sub-meter accuracy), NOT equivalent to the multi-hour
        static PPP positioning used to determine the definitive
        installation position. Use it for a quick estimate or minor
        relocations, not as a substitute for PPP.
        """
        if self.survey_running:
            return
        self.survey_running = True
        self.survey_cancel_event = threading.Event()
        self.mqtt.publish(f"{BASE}/survey_in/state", "running", retain=True)
        print(f"[survey-in] started for {self.survey_duration}s", flush=True)

        self.driver.set_rover_mode(self.rtcm_port, self.baud)

        samples = []
        deadline = time.time() + self.survey_duration
        cancelled = False
        try:
            if self.nmea_port == self.rtcm_port:
                read_line, close_reader = self._relay_line_reader()
            else:
                ser = serial.Serial(self.nmea_port, self.baud, timeout=1)
                read_line, close_reader = (lambda: ser.readline().decode(errors="replace")), ser.close
            try:
                last_progress = 0
                while time.time() < deadline:
                    if self.survey_cancel_event.is_set():
                        cancelled = True
                        break
                    now = time.time()
                    if now - last_progress >= 1:
                        self.mqtt.publish(f"{BASE}/survey_in_remaining/state",
                                           max(0, int(deadline - now)), retain=True)
                        last_progress = now
                    line = read_line()
                    fix = nmea.parse_gga(line)
                    if fix and fix["lat"] is not None and fix["quality"] > 0:
                        samples.append((fix["lat"], fix["lon"], fix["alt"]))
            finally:
                close_reader()
        except Exception as e:
            print("[survey-in] error:", e, flush=True)
            self.mqtt.publish(f"{BASE}/survey_in/state", "error", retain=True)
            self.mqtt.publish(f"{BASE}/survey_in_remaining/state", 0, retain=True)
            self.survey_running = False
            return

        self.mqtt.publish(f"{BASE}/survey_in_remaining/state", 0, retain=True)

        if cancelled:
            print(f"[survey-in] cancelled by the user after {len(samples)} samples", flush=True)
            self.mqtt.publish(f"{BASE}/survey_in/state", "cancelled", retain=True)
            self.survey_running = False
            return

        if len(samples) < 10:
            print("[survey-in] failed: too few fixes collected", flush=True)
            self.mqtt.publish(f"{BASE}/survey_in/state", "error", retain=True)
            self.survey_running = False
            return

        avg_lat = statistics.mean(s[0] for s in samples)
        avg_lon = statistics.mean(s[1] for s in samples)
        heights = [s[2] for s in samples if s[2] is not None]
        avg_height = statistics.mean(heights) if heights else 0.0

        self.driver.set_fixed_base(self.rtcm_port, self.baud, avg_lat, avg_lon, avg_height)
        print(f"[survey-in] completed: {avg_lat:.7f}, {avg_lon:.7f}, {avg_height:.2f} "
              f"({len(samples)} samples)", flush=True)
        self.save_position_backup(avg_lat, avg_lon, avg_height, "survey_in",
                                   duration_sec=self.survey_duration, num_samples=len(samples))
        self.mqtt.publish(f"{BASE}/survey_in/state", "done", retain=True)
        self.survey_running = False

    # -------------------------------------------------------------- PPP

    def run_ppp_campaign(self):
        """PPP-static campaign: waits ppp_duration_hours hours while
        accumulating the raw log (already always active via str2str),
        then processes the window with RTKLIB (convbin + rnx2rtkp
        PPP-static + IGS products) and applies the resulting position as
        the fixed base. Expected accuracy: centimeter-level with logs of
        several hours, unlike the quick survey-in which is only
        meter-level.

        ppp_status goes through: logging -> processing (convbin, doesn't
        need network) -> waiting_for_products (only entered if IGS
        products aren't published yet for such a recent date - retried
        every PPP_PRODUCT_RETRY_INTERVAL_S, bounded by
        raw_log_retention_hours) -> processing again (rnx2rtkp) -> done.
        Cancellable at every step, including while waiting.

        "done" here means a fix was applied - usually from "rapid" (RAP)
        products, since "final" (FIN, more precise) ones take ~11-18 days
        and aren't worth blocking on. If RAP was used, this schedules a
        separate run_ppp_refinement() background task (see its docstring)
        that checks daily for FIN becoming available and exposes a
        refined position later without blocking a new campaign from being
        started in the meantime."""
        if self.ppp_running:
            return
        self.ppp_running = True
        self.ppp_cancel_event = threading.Event()
        # Supersede (and let stop on its own) any refinement thread still
        # checking daily for FIN products from a previous campaign - this
        # new campaign will schedule its own if it also needs one.
        self.ppp_refinement_id += 1
        self.mqtt.publish(f"{BASE}/ppp_refinement_status/state", "idle", retain=True)
        # A fresh campaign wipes ppp_campaign_workdir below regardless, so
        # any pending retry_ppp_computation() state from a previous failed
        # attempt is no longer valid.
        self.ppp_retry_state = None
        start_ts = time.time()
        duration_s = self.ppp_duration_hours * 3600
        deadline = start_ts + duration_s
        self.mqtt.publish(f"{BASE}/ppp_status/state", "logging", retain=True)
        print(f"[ppp] campaign started, duration {self.ppp_duration_hours}h", flush=True)

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if self.ppp_cancel_event.is_set():
                print("[ppp] campaign cancelled by the user during logging", flush=True)
                self.mqtt.publish(f"{BASE}/ppp_status/state", "cancelled", retain=True)
                self.mqtt.publish(f"{BASE}/ppp_remaining/state", 0, retain=True)
                self.ppp_running = False
                return
            self.mqtt.publish(f"{BASE}/ppp_remaining/state", int(remaining), retain=True)
            time.sleep(min(1, remaining))
        end_ts = time.time()
        self.mqtt.publish(f"{BASE}/ppp_remaining/state", 0, retain=True)

        self.mqtt.publish(f"{BASE}/ppp_status/state", "processing", retain=True)
        workdir = self._fresh_ppp_workdir()
        try:
            obs_path, nav_path, dates, raw_files = self._convert_raw_window_to_rinex(start_ts, end_ts, workdir)
        except Exception as e:
            print("[ppp] error:", e, flush=True)
            self.mqtt.publish(f"{BASE}/ppp_status/state", "error", retain=True)
            shutil.rmtree(workdir, ignore_errors=True)
            self.ppp_running = False
            return

        wait_deadline = end_ts + self.raw_log_retention_hours * 3600
        result = self._wait_for_products(dates, workdir, wait_deadline)
        if result is None:  # already published its own status (cancelled/error)
            shutil.rmtree(workdir, ignore_errors=True)
            self.ppp_running = False
            return
        sp3_paths, clk_paths, atx_path, tiers_used = result

        self._compute_and_finish_ppp(dates, obs_path, nav_path, sp3_paths, clk_paths, atx_path,
                                      workdir, raw_files, tiers_used, self.ppp_duration_hours)

    def reprocess_existing_logs(self):
        """Reprocesses whatever raw log files are still on disk (governed
        by raw_log_retention_hours) as a PPP-static campaign, without a
        new logging phase. Recovers a campaign whose intermediate files
        were already lost - the whole point of retry_ppp_computation is
        to avoid ever needing this again for a *computation* failure, but
        it doesn't help if the files were lost some other way (e.g. an
        add-on version before 0.2.20, or the retention window rotating
        out a raw file while waiting for IGS products). Found worth
        adding after a real campaign's intermediate files were lost this
        way before retry_ppp_computation existed.

        Shares ppp_running/ppp_cancel_event/ppp_retry_state with
        run_ppp_campaign: only one PPP operation (campaign, reprocess, or
        computation retry) runs at a time, and starting this one
        supersedes/discards whatever the other left behind, same as
        starting a new campaign does."""
        if self.ppp_running:
            return
        self.ppp_running = True
        self.ppp_cancel_event = threading.Event()
        self.ppp_refinement_id += 1
        self.mqtt.publish(f"{BASE}/ppp_refinement_status/state", "idle", retain=True)
        self.ppp_retry_state = None
        self.mqtt.publish(f"{BASE}/ppp_status/state", "processing", retain=True)
        print("[ppp] reprocessing existing raw logs (no new logging phase)", flush=True)

        workdir = self._fresh_ppp_workdir()
        now = time.time()
        start_ts = now - self.raw_log_retention_hours * 3600
        try:
            obs_path, nav_path, dates, raw_files = self._convert_raw_window_to_rinex(start_ts, now, workdir)
        except Exception as e:
            print("[ppp] error:", e, flush=True)
            self.mqtt.publish(f"{BASE}/ppp_status/state", "error", retain=True)
            shutil.rmtree(workdir, ignore_errors=True)
            self.ppp_running = False
            return

        wait_deadline = now + self.raw_log_retention_hours * 3600
        result = self._wait_for_products(dates, workdir, wait_deadline)
        if result is None:
            shutil.rmtree(workdir, ignore_errors=True)
            self.ppp_running = False
            return
        sp3_paths, clk_paths, atx_path, tiers_used = result

        # len(raw_files) approximates the covered duration in hours (files
        # rotate hourly) - only used for the position backup's metadata.
        self._compute_and_finish_ppp(dates, obs_path, nav_path, sp3_paths, clk_paths, atx_path,
                                      workdir, raw_files, tiers_used, len(raw_files))

    def _fresh_ppp_workdir(self):
        """Derived from RAW_LOG_DIR (not hardcoded "/data") so that
        monkeypatching main.RAW_LOG_DIR (as the test suite and the
        standalone test harness do, to run outside /data) also redirects
        this workdir - otherwise it silently kept trying to create it
        under the real "/data", which doesn't exist/isn't writable
        outside the container. Not a tempfile.TemporaryDirectory
        (auto-deleted on exit): it must survive the waiting_for_products
        retry loop, which can run for hours."""
        workdir = Path(RAW_LOG_DIR).parent / "ppp_campaign_workdir"
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True)
        return workdir

    def _convert_raw_window_to_rinex(self, start_ts, end_ts, workdir):
        """Raw log files covering [start_ts, end_ts] -> concatenated ->
        RINEX (convbin) -> observation dates. Raises on failure (no files
        found for the window, convbin error) - the caller decides how to
        report/clean up."""
        raw_files = ppp.collect_raw_files(RAW_LOG_DIR, start_ts, end_ts)
        if not raw_files:
            raise RuntimeError("No raw log file found for the given window")
        raw_concat = workdir / "campaign.rtcm3"
        ppp.concat_raw_files(raw_files, raw_concat)
        obs_path, nav_path = ppp.convbin(str(raw_concat), str(workdir))
        dates = ppp.parse_obs_dates(obs_path)
        return obs_path, nav_path, dates, raw_files

    def _wait_for_products(self, dates, workdir, wait_deadline):
        """Polls ppp.fetch_precise_products every
        PPP_PRODUCT_RETRY_INTERVAL_S until it succeeds, the operation is
        cancelled, or wait_deadline passes - IGS precise products may not
        be published yet for such a recent date (FIN: ~11-18 days, RAP:
        ~17-41h after the observation date), found from a real campaign
        that used to fail outright the moment it started processing.
        wait_deadline is normally end_ts + raw_log_retention_hours:
        retrying past that point is pointless, cleanup_raw_logs() will
        already have deleted the source raw log by then anyway.

        Returns (sp3_paths, clk_paths, atx_path, tiers_used) on success,
        or None if cancelled/timed out - in that case ppp_status
        ("cancelled" or "error") and ppp_remaining have already been
        published; the caller only needs to clean up workdir and reset
        ppp_running."""
        while True:
            try:
                return ppp.fetch_precise_products(dates, workdir)
            except Exception as e:
                if self.ppp_cancel_event.is_set():
                    print("[ppp] cancelled by the user while waiting for IGS products", flush=True)
                    self.mqtt.publish(f"{BASE}/ppp_status/state", "cancelled", retain=True)
                    self.mqtt.publish(f"{BASE}/ppp_remaining/state", 0, retain=True)
                    return None
                remaining_wait = wait_deadline - time.time()
                if remaining_wait <= 0:
                    print(f"[ppp] error: IGS products still unavailable after waiting until the raw "
                          f"log retention window closed ({e})", flush=True)
                    self.mqtt.publish(f"{BASE}/ppp_status/state", "error", retain=True)
                    self.mqtt.publish(f"{BASE}/ppp_remaining/state", 0, retain=True)
                    return None
                print(f"[ppp] IGS products not available yet ({e}), retrying in "
                      f"{PPP_PRODUCT_RETRY_INTERVAL_S}s...", flush=True)
                self.mqtt.publish(f"{BASE}/ppp_status/state", "waiting_for_products", retain=True)
                retry_deadline = min(time.time() + PPP_PRODUCT_RETRY_INTERVAL_S, wait_deadline)
                while time.time() < retry_deadline:
                    if self.ppp_cancel_event.is_set():
                        break
                    self.mqtt.publish(f"{BASE}/ppp_remaining/state",
                                       max(0, int(wait_deadline - time.time())), retain=True)
                    time.sleep(min(1, retry_deadline - time.time()))
                self.mqtt.publish(f"{BASE}/ppp_status/state", "processing", retain=True)

    def _compute_and_finish_ppp(self, dates, obs_path, nav_path, sp3_paths, clk_paths, atx_path,
                                 workdir, raw_files, tiers_used, duration_hours):
        """The final, purely computational step (rnx2rtkp + parsing the
        result), shared by run_ppp_campaign (first attempt),
        reprocess_existing_logs, and retry_ppp_computation (retries only
        this step, reusing the same already-downloaded IGS products). On
        success: applies/persists the position, permanently archives the
        exact raw log files behind it (see _archive_ppp_source_logs) and,
        if a less precise tier (RAP) was used, schedules background
        refinement (see run_ppp_refinement). On failure: keeps everything
        needed to retry via button.retry_ppp_computation instead of
        discarding already-downloaded products and forcing a full re-log
        + re-download - found worth doing from a real campaign that
        finally got IGS products after ~26 hourly retries, only to fail
        at this last step over a config bug.

        duration_hours is only informational (position_backup metadata):
        the configured campaign duration for a normal run, or an estimate
        of the reprocessed window's span otherwise - passed in rather
        than read from self.ppp_duration_hours so it stays accurate for
        reprocess_existing_logs/retry_ppp_computation, which don't
        necessarily match that number entity's current value."""
        try:
            pos_path = ppp.run_rnx2rtkp(obs_path, nav_path, sp3_paths, clk_paths, atx_path, workdir)
            lat, lon, height = ppp.parse_last_position(pos_path)
        except Exception as e:
            print("[ppp] error:", e, flush=True)
            print("[ppp] IGS products were already downloaded - keeping them so "
                  "button.retry_ppp_computation can retry just this step", flush=True)
            self.ppp_retry_state = {
                "dates": dates, "obs_path": obs_path, "nav_path": nav_path,
                "sp3_paths": sp3_paths, "clk_paths": clk_paths, "atx_path": atx_path,
                "workdir": workdir, "raw_files": raw_files, "tiers_used": tiers_used,
                "duration_hours": duration_hours,
            }
            self.mqtt.publish(f"{BASE}/ppp_status/state", "error", retain=True)
            self.mqtt.publish(f"{BASE}/ppp_remaining/state", 0, retain=True)
            self.ppp_running = False
            return

        self.ppp_retry_state = None
        self.driver.set_fixed_base(self.rtcm_port, self.baud, lat, lon, height)
        print(f"[ppp] completed: {lat:.8f}, {lon:.8f}, {height:.3f} "
              f"(tiers used: {tiers_used})", flush=True)
        backup_data = self.save_position_backup(lat, lon, height, "ppp",
                                                 duration_hours=duration_hours, num_raw_files=len(raw_files))
        self._archive_ppp_source_logs(raw_files, backup_data["computed_at"])
        self.mqtt.publish(f"{BASE}/ppp_status/state", "done", retain=True)
        self.mqtt.publish(f"{BASE}/ppp_remaining/state", 0, retain=True)
        self.mqtt.publish(f"{BASE}/manual_lat/state", f"{lat:.8f}", retain=True)
        self.mqtt.publish(f"{BASE}/manual_lon/state", f"{lon:.8f}", retain=True)
        self.mqtt.publish(f"{BASE}/manual_height/state", f"{height:.3f}", retain=True)
        self.manual_lat, self.manual_lon, self.manual_height = lat, lon, height
        self.ppp_running = False

        if all(tier == "FIN" for tier in tiers_used.values()):
            # Already the best available tier - nothing to refine later.
            shutil.rmtree(workdir, ignore_errors=True)
            self.mqtt.publish(f"{BASE}/ppp_refinement_status/state", "idle", retain=True)
            return

        # A less precise tier (RAP) was used for at least one date: "final"
        # products (more precise) may still show up later, published
        # ~11-18 days after the observation date. Move the workdir to a
        # stable location (this campaign's own workdir is about to be
        # reusable by the next campaign) and check for them once a day in
        # the background - checking as often as the main retry loop above
        # would be pointless at that latency. Superseded if a new campaign
        # starts before this finishes (self.ppp_refinement_id changes,
        # bumped by both run_ppp_campaign and retry_ppp_computation at
        # their start).
        refinement_workdir = Path(RAW_LOG_DIR).parent / "ppp_refinement_workdir"
        shutil.rmtree(refinement_workdir, ignore_errors=True)
        workdir.rename(refinement_workdir)
        obs_path = refinement_workdir / Path(obs_path).name
        nav_path = refinement_workdir / Path(nav_path).name
        refinement_id = self.ppp_refinement_id
        threading.Thread(
            target=self.run_ppp_refinement,
            args=(refinement_id, dates, obs_path, nav_path, refinement_workdir),
            daemon=True,
        ).start()

    def _archive_ppp_source_logs(self, raw_files, computed_at):
        """Permanently preserves a copy of the exact raw log files that
        determined the currently-applied PPP position, in a directory
        RAW_LOG_DIR's parent/ppp_source_logs/<computed_at>/ - unlike the
        continuous raw log in RAW_LOG_DIR, this copy is never touched by
        cleanup_raw_logs()'s raw_log_retention_hours-based deletion, and
        is never pruned automatically by this add-on: the source data
        behind an actual applied position is worth more than routine log
        retention. One archive per successful computation (campaign,
        reprocess, or computation retry) - old ones aren't replaced or
        removed, so disk usage grows slowly over time with repeated use;
        size accordingly (raw logs are small, on the order of ~1MB/hour
        of logging, see the README)."""
        archive_dir = Path(RAW_LOG_DIR).parent / "ppp_source_logs" / computed_at.replace(":", "-")
        archive_dir.mkdir(parents=True, exist_ok=True)
        archived = 0
        for raw_file in raw_files:
            try:
                shutil.copy2(raw_file, archive_dir / Path(raw_file).name)
                archived += 1
            except OSError as e:
                # e.g. cleanup_raw_logs() already rotated this file out
                # before a delayed retry_ppp_computation() ran - archive
                # whatever's still there rather than failing outright.
                print(f"[ppp] warning: could not archive {raw_file}: {e}", flush=True)
        print(f"[ppp] archived {archived}/{len(raw_files)} raw log file(s) "
              f"used for this position to {archive_dir}", flush=True)

    def retry_ppp_computation(self):
        """Retries just the final computation step (see
        _compute_and_finish_ppp) using the same already-downloaded IGS
        products from the last failed attempt - no re-logging, no
        re-downloading. A no-op if there's nothing to retry (e.g. a new
        campaign already started, which wipes ppp_campaign_workdir and,
        with it, implicitly supersedes the retry state - see
        run_ppp_campaign)."""
        if self.ppp_retry_state is None or self.ppp_running:
            return
        state = self.ppp_retry_state
        self.ppp_running = True
        self.ppp_refinement_id += 1
        self.mqtt.publish(f"{BASE}/ppp_refinement_status/state", "idle", retain=True)
        self.mqtt.publish(f"{BASE}/ppp_status/state", "processing", retain=True)
        self._compute_and_finish_ppp(
            state["dates"], state["obs_path"], state["nav_path"], state["sp3_paths"],
            state["clk_paths"], state["atx_path"], state["workdir"], state["raw_files"],
            state["tiers_used"], state["duration_hours"])

    def run_ppp_refinement(self, refinement_id, dates, obs_path, nav_path, workdir):
        """Runs after a campaign completes using a less precise IGS
        product tier (RAP) than the best available (FIN). Checks once a
        day whether "final" products have been published yet for the
        campaign's date(s) - checking as often as the main campaign's
        retry loop would be pointless at FIN's ~11-18 day latency. If/when
        they show up, computes the refined position and exposes it via
        the same manual position fields used everywhere else in this
        add-on - population only, consistent with how this add-on never
        changes the base's live position without an explicit user action
        (button.apply_manual_position remains required to actually send
        it to the receiver).

        Superseded by a new campaign starting in the meantime
        (self.ppp_refinement_id no longer matches refinement_id, checked
        at the top of every wait cycle) - stops and cleans up instead of
        racing the new campaign for the same workdir name."""
        self.mqtt.publish(f"{BASE}/ppp_refinement_status/state", "waiting_for_final", retain=True)
        print(f"[ppp] refinement: checking daily for FIN products for {[str(d) for d in dates]}", flush=True)
        while self.ppp_refinement_id == refinement_id:
            waited = 0
            superseded = False
            while waited < PPP_REFINEMENT_CHECK_INTERVAL_S:
                if self.ppp_refinement_id != refinement_id:
                    superseded = True
                    break
                step = min(1, PPP_REFINEMENT_CHECK_INTERVAL_S - waited)
                time.sleep(step)
                waited += step
            if superseded:
                break
            try:
                sp3_paths, clk_paths, atx_path, _ = ppp.fetch_precise_products(
                    dates, workdir, products=("FIN",))
            except Exception as e:
                print(f"[ppp] refinement: FIN products not available yet ({e}), "
                      f"retrying in {PPP_REFINEMENT_CHECK_INTERVAL_S}s", flush=True)
                continue
            if self.ppp_refinement_id != refinement_id:
                break
            try:
                pos_path = ppp.run_rnx2rtkp(obs_path, nav_path, sp3_paths, clk_paths, atx_path, workdir)
                lat, lon, height = ppp.parse_last_position(pos_path)
            except Exception as e:
                print(f"[ppp] refinement error: {e}", flush=True)
                self.mqtt.publish(f"{BASE}/ppp_refinement_status/state", "error", retain=True)
                shutil.rmtree(workdir, ignore_errors=True)
                return
            print(f"[ppp] refinement: FIN-based position available: {lat:.8f}, {lon:.8f}, "
                  f"{height:.3f} (not applied automatically)", flush=True)
            self.manual_lat, self.manual_lon, self.manual_height = lat, lon, height
            self.mqtt.publish(f"{BASE}/manual_lat/state", f"{lat:.8f}", retain=True)
            self.mqtt.publish(f"{BASE}/manual_lon/state", f"{lon:.8f}", retain=True)
            self.mqtt.publish(f"{BASE}/manual_height/state", f"{height:.3f}", retain=True)
            self.mqtt.publish(f"{BASE}/ppp_refinement_status/state", "available", retain=True)
            shutil.rmtree(workdir, ignore_errors=True)
            return
        shutil.rmtree(workdir, ignore_errors=True)

    def _resume_ppp_refinement_if_any(self):
        """Called once at add-on startup. Unlike ppp_campaign_workdir
        (always wiped, see run()), ppp_refinement_workdir is deliberately
        NOT deleted on restart: it can be waiting for FIN products for up
        to ~18 days, almost certainly spanning at least one add-on/Home
        Assistant restart in practice, and the whole point of that wait is
        to survive until the next campaign starts - not until the next
        restart. Resumes checking from the persisted RINEX files if
        present; discards the directory only if it's missing what's
        needed to resume (e.g. an older add-on version's leftovers)."""
        workdir = Path(RAW_LOG_DIR).parent / "ppp_refinement_workdir"
        obs_path, nav_path = workdir / "campaign.obs", workdir / "campaign.nav"
        if not (obs_path.exists() and nav_path.exists()):
            shutil.rmtree(workdir, ignore_errors=True)
            return
        try:
            dates = ppp.parse_obs_dates(str(obs_path))
        except Exception as e:
            print(f"[ppp] discarding unresumable refinement workdir ({e})", flush=True)
            shutil.rmtree(workdir, ignore_errors=True)
            return
        self.ppp_refinement_id += 1
        print(f"[ppp] resuming refinement check for {[str(d) for d in dates]} after restart", flush=True)
        threading.Thread(
            target=self.run_ppp_refinement,
            args=(self.ppp_refinement_id, dates, obs_path, nav_path, workdir),
            daemon=True,
        ).start()

    def apply_manual_position(self):
        if None in (self.manual_lat, self.manual_lon, self.manual_height):
            print("[main] manual position incomplete: set lat/lon/height before applying", flush=True)
            return
        self.driver.set_fixed_base(self.rtcm_port, self.baud, self.manual_lat, self.manual_lon, self.manual_height)
        print("[main] manual position applied:",
              self.manual_lat, self.manual_lon, self.manual_height, flush=True)
        self.save_position_backup(self.manual_lat, self.manual_lon, self.manual_height, "manual")

    # -------------------------------------------------------------- monitor

    def monitor_nmea(self):
        """Main NMEA reading loop. Dispatches to one of two strategies
        depending on whether rtcm_port == nmea_port (see
        needs_internal_relay() for why a shared physical port can't be
        opened a second time directly)."""
        if self.nmea_port == self.rtcm_port:
            self._monitor_nmea_via_relay()
        else:
            self._monitor_nmea_via_serial()

    def _monitor_nmea_via_serial(self):
        """Used when nmea_port is a physically separate port from
        rtcm_port (e.g. a receiver wired with two independent UARTs): a
        direct serial.Serial() here doesn't compete with str2str, which
        only reads rtcm_port. Automatic reconnection: if the serial port
        disappears (USB unplugged) or nothing arrives for
        SERIAL_SILENCE_TIMEOUT_S seconds, it signals the disconnection on
        MQTT and retries at regular intervals, without ever terminating
        the add-on process."""
        while True:
            wait_for_serial_port(self.nmea_port, "NMEA")
            try:
                with serial.Serial(self.nmea_port, self.baud, timeout=1) as ser:
                    last_data_ts = time.time()
                    while True:
                        line = ser.readline().decode(errors="replace")
                        if not line:
                            if not os.path.exists(self.nmea_port):
                                raise serial.SerialException("the serial port no longer exists")
                            if time.time() - last_data_ts > SERIAL_SILENCE_TIMEOUT_S:
                                raise serial.SerialException(
                                    f"no data for over {SERIAL_SILENCE_TIMEOUT_S}s")
                            continue
                        last_data_ts = time.time()
                        self.set_device_connected(True)
                        self._process_nmea_line(line)
            except (serial.SerialException, OSError) as e:
                self.set_device_connected(False, reason=str(e))
                time.sleep(SERIAL_RETRY_INTERVAL_S)

    def _monitor_nmea_via_relay(self):
        """Used when rtcm_port == nmea_port: str2str is already the sole
        process reading the physical serial port (started once in run()
        and kept alive for the app's lifetime). A second direct
        serial.Serial() here would race with it for bytes on the same
        character device - verified on real hardware: without this,
        device_connected flapped ON/OFF every ~15-20s with corrupted/lost
        NMEA. Instead, subscribe as a client of the same internal relay
        used for the local NTRIP caster (str2str -out tcpsvr://... ->
        caster.run_relay_receiver), which carries the raw byte stream
        unfiltered: str2str relays bytes verbatim, so RTCM3 and NMEA
        arrive interleaved on it (verified against a real capture
        containing readable $GNGGA lines).

        Known limitation: this only receives data while str2str is
        actually running. If validate_stream_budget() rejected the
        configuration (too many outputs), str2str never starts and this
        loop will silently see no data (device_connected stays OFF) even
        if the receiver itself is fine - check sensor.configuration_error
        in that case."""
        while True:
            read_line, close_reader = self._relay_line_reader()
            last_data_ts = time.time()
            try:
                while True:
                    line = read_line()
                    if not line:
                        if time.time() - last_data_ts > SERIAL_SILENCE_TIMEOUT_S:
                            raise OSError(f"no data for over {SERIAL_SILENCE_TIMEOUT_S}s via internal relay")
                        continue
                    last_data_ts = time.time()
                    self.set_device_connected(True)
                    self._process_nmea_line(line)
            except OSError as e:
                self.set_device_connected(False, reason=str(e))
            finally:
                close_reader()
            time.sleep(SERIAL_RETRY_INTERVAL_S)

    # ----------------------------------------------------------------- run

    def run(self):
        print(f"[main] receiver driver: {self.receiver_type} "
              f"({getattr(self.driver, 'NAME', '?')})", flush=True)
        # A PPP campaign's workdir (see run_ppp_campaign) can now survive
        # for hours across the waiting_for_products retry loop, so an
        # add-on restart while one is in progress would otherwise leave it
        # behind forever - any such in-progress campaign is lost anyway on
        # restart (no state persistence), so there's nothing left to clean
        # up but the leftover directory.
        shutil.rmtree(Path(RAW_LOG_DIR).parent / "ppp_campaign_workdir", ignore_errors=True)
        # Start the Ingress web panel and the raw-log cleanup first, before
        # anything that can block for a long time (MQTT connect retry,
        # configure_receiver() waiting for the USB): both the MQTT broker
        # and the receiver may take a while to become available, and while
        # they do we still want the skyplot panel reachable (showing "not
        # connected") instead of Ingress returning a 502 with nothing
        # listening yet. Neither thread touches self.mqtt.
        threading.Thread(target=self.cleanup_raw_logs, daemon=True).start()
        threading.Thread(target=start_webserver, args=(self.state, nmea.fix_label, WEBUI_PORT), daemon=True).start()

        mqtt_host = os.environ.get("MQTT_HOST", "localhost")
        mqtt_port = int(os.environ.get("MQTT_PORT", 1883))
        while True:
            try:
                self.mqtt.connect(mqtt_host, mqtt_port, keepalive=30)
                break
            except OSError as e:
                print(f"[main] MQTT broker unreachable ({mqtt_host}:{mqtt_port}): {e}. "
                      f"Install/start the Mosquitto broker add-on (or configure an external broker). "
                      f"Retrying in {MQTT_RETRY_INTERVAL_S}s...", flush=True)
                time.sleep(MQTT_RETRY_INTERVAL_S)
        self.mqtt.loop_start()
        self._resume_ppp_refinement_if_any()

        self.configure_receiver()
        try:
            self.validate_stream_budget()
        except ValueError as e:
            print(f"[main] {e}", flush=True)
            self.mqtt.publish(f"{BASE}/config_error/state", str(e), retain=True)
        else:
            self.mqtt.publish(f"{BASE}/config_error/state", "", retain=True)
            if self.needs_internal_relay():
                # run_relay_receiver() connects to str2str's own
                # tcpsvr://... as a client and retries until it's up, so
                # start order relative to start_str2str() doesn't matter.
                threading.Thread(target=caster.run_relay_receiver, args=(self.broadcaster,), daemon=True).start()
            self.start_str2str()
            threading.Thread(target=self.watchdog_str2str, daemon=True).start()

        if self.caster_enabled:
            threading.Thread(target=caster.run_caster_server,
                              args=(self.broadcaster, self.caster_mountpoint, self.caster_user,
                                    self.caster_password, caster.CASTER_PORT, self.caster_max_clients),
                              daemon=True).start()
            threading.Thread(target=self.publish_caster_clients, daemon=True).start()

        self.monitor_nmea()  # main loop, blocking


if __name__ == "__main__":
    App(load_options()).run()
