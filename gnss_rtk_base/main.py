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
import statistics
import subprocess
import tempfile
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
        self.broadcaster = caster.Broadcaster() if self.caster_enabled else None

        self.str2str_proc = None
        self.survey_running = False
        self.ppp_running = False
        self.survey_cancel_event = None
        self.ppp_cancel_event = None
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
        """Configures RTCM and NMEA on the module, waiting for the serial
        ports to exist (useful if the USB is not yet enumerated when the
        add-on starts) and retrying on error instead of crashing the
        whole add-on."""
        for port, label in ((self.rtcm_port, "RTCM"), (self.nmea_port, "NMEA")):
            wait_for_serial_port(port, label)

        while True:
            try:
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

    def output_stream_plan(self):
        """Labels for the -out streams passed to str2str, in the same
        order in which they are added by build_str2str_cmd(). Used to
        interpret the 5-character status string that str2str prints
        periodically on stderr: index 0 = serial input, index i = the
        i-th -out in this same order (verified by reading
        strsvrstat()/str2str.c in RTKLIB and with a compiled binary)."""
        labels = [f"{c['host']}:{c['port']}/{c.get('mountpoint', '')}" for c in self.active_casters()]
        labels.append("raw log")
        if self.caster_enabled:
            labels.append("local caster")
        return labels

    def validate_stream_budget(self):
        """RTKLIB limits str2str to MAXSTR=5 total streams (1 input + 4
        outputs, verified in the app/consapp/str2str/str2str.c source):
        beyond the fourth -out, extra arguments are silently and
        unpredictably ignored, not with a clear error. Better to fail
        loudly here than discover it at runtime."""
        needed = len(self.output_stream_plan())
        if needed > 4:
            raise ValueError(
                f"Too many outputs configured for str2str: {needed} "
                f"({len(self.active_casters())} NTRIP caster(s) + 1 raw log"
                f"{' + 1 local caster' if self.caster_enabled else ''}), "
                f"the maximum supported by RTKLIB is 4. Reduce the number "
                f"of entries in ntrip_casters or disable caster_enabled."
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
        if self.caster_enabled:
            # str2str connects to our internal relay (caster.py) and
            # forwards the stream to us, which we re-distribute to
            # connected rovers.
            cmd += ["-out", f"tcpcli://127.0.0.1:{caster.INTERNAL_RELAY_PORT}"]
        return cmd

    def start_str2str(self):
        cmd = self.build_str2str_cmd()
        print("[main] starting str2str:", " ".join(cmd), flush=True)
        self.str2str_proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self.monitor_str2str_status, args=(self.str2str_proc,), daemon=True).start()

    def watchdog_str2str(self):
        while True:
            time.sleep(10)
            if self.str2str_proc is not None and self.str2str_proc.poll() is not None:
                print("[main] str2str terminated unexpectedly, restarting...", flush=True)
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
                      "ppp_duration_hours/set", "ppp_start/set", "ppp_cancel/set"):
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
        the data)."""
        data = position_backup.save(lat, lon, height, method, self.receiver_type, **extra)
        self.mqtt.publish(f"{BASE}/position_backup/state", data["computed_at"], retain=True)
        self.mqtt.publish(f"{BASE}/position_backup/attributes", json.dumps(data), retain=True)

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
            with serial.Serial(self.nmea_port, self.baud, timeout=1) as ser:
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
                    line = ser.readline().decode(errors="replace")
                    fix = nmea.parse_gga(line)
                    if fix and fix["lat"] is not None and fix["quality"] > 0:
                        samples.append((fix["lat"], fix["lon"], fix["alt"]))
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
        meter-level."""
        if self.ppp_running:
            return
        self.ppp_running = True
        self.ppp_cancel_event = threading.Event()
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
        try:
            with tempfile.TemporaryDirectory(dir="/data") as workdir:
                workdir = Path(workdir)
                raw_files = ppp.collect_raw_files(RAW_LOG_DIR, start_ts, end_ts)
                if not raw_files:
                    raise RuntimeError("No raw log file found for the campaign window")
                raw_concat = workdir / "campaign.rtcm3"
                ppp.concat_raw_files(raw_files, raw_concat)

                obs_path, nav_path = ppp.convbin(str(raw_concat), str(workdir))
                dates = ppp.parse_obs_dates(obs_path)
                sp3_paths, clk_paths, atx_path = ppp.fetch_precise_products(dates, workdir)
                pos_path = ppp.run_rnx2rtkp(obs_path, nav_path, sp3_paths, clk_paths, atx_path, workdir)
                lat, lon, height = ppp.parse_last_position(pos_path)
        except Exception as e:
            print("[ppp] error:", e, flush=True)
            self.mqtt.publish(f"{BASE}/ppp_status/state", "error", retain=True)
            self.ppp_running = False
            return

        self.driver.set_fixed_base(self.rtcm_port, self.baud, lat, lon, height)
        print(f"[ppp] completed: {lat:.8f}, {lon:.8f}, {height:.3f}", flush=True)
        self.save_position_backup(lat, lon, height, "ppp",
                                   duration_hours=self.ppp_duration_hours, num_raw_files=len(raw_files))
        self.mqtt.publish(f"{BASE}/ppp_status/state", "done", retain=True)
        self.mqtt.publish(f"{BASE}/manual_lat/state", f"{lat:.8f}", retain=True)
        self.mqtt.publish(f"{BASE}/manual_lon/state", f"{lon:.8f}", retain=True)
        self.mqtt.publish(f"{BASE}/manual_height/state", f"{height:.3f}", retain=True)
        self.manual_lat, self.manual_lon, self.manual_height = lat, lon, height
        self.ppp_running = False

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
        """Main NMEA reading loop, with automatic reconnection: if the
        serial port disappears (USB unplugged) or nothing arrives for
        SERIAL_SILENCE_TIMEOUT_S seconds, it signals the disconnection on
        MQTT and retries at regular intervals, without ever terminating
        the add-on process."""
        while True:
            wait_for_serial_port(self.nmea_port, "NMEA")
            try:
                with serial.Serial(self.nmea_port, self.baud, timeout=1) as ser:
                    last_publish = 0
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

                        gga = nmea.parse_gga(line)
                        if gga:
                            self.state.update_gga(gga)
                            now = time.time()
                            if now - last_publish > 1:
                                self.mqtt.publish(f"{BASE}/fix_status/state",
                                                   nmea.fix_label(gga["quality"]), retain=True)
                                self.mqtt.publish(f"{BASE}/satellites/state", gga["num_sv"], retain=True)
                                last_publish = now
                            continue
                        gst = nmea.parse_gst(line)
                        if gst:
                            self.state.update_gst(gst["accuracy_m"])
                            self.mqtt.publish(f"{BASE}/accuracy/state", gst["accuracy_m"], retain=True)
                            continue
                        gsv = nmea.parse_gsv(line)
                        if gsv:
                            self.state.update_gsv(gsv)
                            continue
                        gsa = nmea.parse_gsa(line)
                        if gsa:
                            self.state.update_gsa(gsa)
            except (serial.SerialException, OSError) as e:
                self.set_device_connected(False, reason=str(e))
                time.sleep(SERIAL_RETRY_INTERVAL_S)

    # ----------------------------------------------------------------- run

    def run(self):
        print(f"[main] receiver driver: {self.receiver_type} "
              f"({getattr(self.driver, 'NAME', '?')})", flush=True)
        # MQTT must be connected before configure_receiver(): the latter
        # may have to wait a long time for the USB to appear, and during
        # that wait we want to be able to publish the "not connected"
        # status instead of staying silent.
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

        # Start the Ingress web panel and the raw-log cleanup before
        # configure_receiver(): that call blocks indefinitely waiting for
        # the receiver's serial port to appear, and while it waits we
        # still want the skyplot panel reachable (showing "not connected")
        # instead of Ingress returning a 502 with nothing listening yet.
        threading.Thread(target=self.cleanup_raw_logs, daemon=True).start()
        threading.Thread(target=start_webserver, args=(self.state, nmea.fix_label, WEBUI_PORT), daemon=True).start()

        self.configure_receiver()
        try:
            self.validate_stream_budget()
        except ValueError as e:
            print(f"[main] {e}", flush=True)
            self.mqtt.publish(f"{BASE}/config_error/state", str(e), retain=True)
        else:
            self.mqtt.publish(f"{BASE}/config_error/state", "", retain=True)
            self.start_str2str()
            threading.Thread(target=self.watchdog_str2str, daemon=True).start()

        if self.caster_enabled:
            threading.Thread(target=caster.run_relay_receiver, args=(self.broadcaster,), daemon=True).start()
            threading.Thread(target=caster.run_caster_server,
                              args=(self.broadcaster, self.caster_mountpoint, self.caster_user,
                                    self.caster_password, caster.CASTER_PORT, self.caster_max_clients),
                              daemon=True).start()
            threading.Thread(target=self.publish_caster_clients, daemon=True).start()

        self.monitor_nmea()  # main loop, blocking


if __name__ == "__main__":
    App(load_options()).run()
