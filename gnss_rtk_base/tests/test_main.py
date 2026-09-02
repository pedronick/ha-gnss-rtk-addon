import datetime as dt
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import caster
import drivers
import main
import position_backup
import ppp
from state import SharedState


class FakeMqtt:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload))

    def username_pw_set(self, *a, **k):
        pass


def _bare_app(**overrides):
    """Builds an App instance bypassing __init__ (which would open a real
    MQTT connection), setting only the attributes needed for pure-logic
    tests (build_str2str_cmd, serial resilience, etc.)."""
    app = main.App.__new__(main.App)
    defaults = dict(
        rtcm_port="/dev/null-fake",
        nmea_port="/dev/null-fake",
        baud=115200,
        driver=drivers.get_driver("unicore_um98x"),
        receiver_type="unicore_um98x",
        ntrip_casters=[],
        caster_enabled=False,
        mqtt=FakeMqtt(),
        state=SharedState(),
        survey_running=False,
        ppp_running=False,
        survey_cancel_event=None,
        ppp_cancel_event=None,
        ppp_refinement_id=0,
        ppp_retry_state=None,
        broadcaster=caster.Broadcaster(),
        _last_fix_publish=0,
    )
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(app, key, value)
    return app


def test_build_str2str_cmd_includes_one_out_per_caster_and_log_file():
    # nmea_port deliberately different from rtcm_port: this test is about
    # the caster/log outputs, decoupled from needs_internal_relay()'s
    # extra -out for the rtcm_port == nmea_port case (covered separately).
    app = _bare_app(nmea_port="/dev/null-fake-nmea", ntrip_casters=[
        {"host": "rtk2go.com", "port": 2101, "mountpoint": "A", "password": "p1"},
        {"host": "caster.example.com", "port": 2101, "mountpoint": "B", "password": "p2"},
        {"host": "", "port": 2101, "mountpoint": "", "password": ""},  # empty slot
    ])
    cmd = app.build_str2str_cmd()

    # str2str wants the device name without "/dev/" (it prepends that
    # itself internally: passing the full path would double it up and
    # str2str would never start). Verified with a real binary.
    assert cmd[:3] == ["str2str", "-in", "serial://null-fake:115200"]
    outs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-out"]
    assert len(outs) == 3  # 2 active casters + 1 log file, the empty slot is ignored
    assert any("rtk2go.com" in o for o in outs)
    assert any("caster.example.com" in o for o in outs)
    assert any(o.startswith("file://") for o in outs)


def test_build_str2str_cmd_ntrips_url_has_no_username():
    """RTKLIB (reqntrip_s in stream.c) only uses the mountpoint password
    for the server/encoder role: a username in the URL would be silently
    ignored. The correct syntax is ntrips://:password@host:port/mountpoint."""
    app = _bare_app(ntrip_casters=[
        {"host": "rtk2go.com", "port": 2101, "mountpoint": "MYMOUNT", "password": "secret"},
    ])
    cmd = app.build_str2str_cmd()
    outs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-out"]
    ntrip_out = next(o for o in outs if o.startswith("ntrips://"))
    assert ntrip_out == "ntrips://:secret@rtk2go.com:2101/MYMOUNT"


def test_build_str2str_cmd_does_not_shadow_caster_module():
    """Regression: build_str2str_cmd used 'caster' as the for-loop
    variable name, shadowing the imported 'caster' module — with
    caster_enabled=True and at least one caster in the list, the line
    referencing caster.INTERNAL_RELAY_PORT failed with AttributeError."""
    app = _bare_app(
        ntrip_casters=[{"host": "rtk2go.com", "port": 2101, "mountpoint": "A", "password": "p"}],
        caster_enabled=True,
    )
    cmd = app.build_str2str_cmd()
    outs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-out"]
    relay_outs = [o for o in outs if o.startswith("tcpsvr://:")]
    assert len(relay_outs) == 1
    assert relay_outs[0] == f"tcpsvr://:{caster.INTERNAL_RELAY_PORT}"


def test_build_str2str_cmd_without_any_caster_configured():
    app = _bare_app(nmea_port="/dev/null-fake-nmea", ntrip_casters=[])
    cmd = app.build_str2str_cmd()
    outs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-out"]
    assert len(outs) == 1  # only the log file
    assert outs[0].startswith("file://")


@pytest.mark.parametrize("port", ["/dev/ttyUSB0", "/dev/ttyACM0"])
def test_build_str2str_cmd_strips_dev_prefix_from_serial_port(port):
    """str2str prepends '/dev/' to the device name itself (openserial()
    in src/stream.c): passing it the full path would produce
    '/dev//dev/...', which doesn't exist, and str2str would never start.
    Verified with a real binary (v2.4.3-b34) during development."""
    app = _bare_app(rtcm_port=port)
    cmd = app.build_str2str_cmd()
    assert cmd[2] == f"serial://{port.removeprefix('/dev/')}:115200"
    assert "/dev/" not in cmd[2]


def test_validate_stream_budget_ok_at_the_limit():
    # 3 casters + 1 log = 4, exactly at RTKLIB's limit (MAXSTR=5: 1 in + 4 out).
    # nmea_port != rtcm_port so needs_internal_relay() doesn't add a 5th
    # output here (that combination is covered by the "raises" test below).
    app = _bare_app(nmea_port="/dev/null-fake-nmea", ntrip_casters=[
        {"host": f"caster{i}.example.com", "port": 2101, "mountpoint": "M", "password": "p"}
        for i in range(3)
    ], caster_enabled=False)
    app.validate_stream_budget()  # must not raise


def test_validate_stream_budget_raises_when_exceeded():
    # 3 casters + 1 log + 1 local caster = 5, exceeds the limit of 4 -out.
    app = _bare_app(ntrip_casters=[
        {"host": f"caster{i}.example.com", "port": 2101, "mountpoint": "M", "password": "p"}
        for i in range(3)
    ], caster_enabled=True)
    with pytest.raises(ValueError, match="Too many outputs"):
        app.validate_stream_budget()


def test_output_stream_plan_order_matches_build_str2str_cmd():
    app = _bare_app(
        ntrip_casters=[{"host": "rtk2go.com", "port": 2101, "mountpoint": "M1", "password": "p"}],
        caster_enabled=True,
    )
    plan = app.output_stream_plan()
    assert plan == ["rtk2go.com:2101/M1", "raw log", "local caster"]


def test_monitor_str2str_status_parses_real_status_line_format():
    """Real line captured by actually running str2str v2.4.3-b34 with one
    reachable output (file) and one unreachable (tcpcli):
    '2026/08/22 20:15:54 [CWC--]        475 B       0 bps (1) recv error (111) '
    """
    app = _bare_app(
        ntrip_casters=[{"host": "unreachable.example.com", "port": 9999, "mountpoint": "M", "password": "p"}],
    )
    line = "2026/08/22 20:15:54 [CWC--]        475 B       0 bps (1) recv error (111) \n"

    class FakeProc:
        args = ["str2str"]
        stderr = iter([line])

    fake_proc = FakeProc()
    app.str2str_proc = fake_proc
    app.monitor_str2str_status(fake_proc)

    published = dict((t, p) for t, p in app.mqtt.published)
    assert published["gnssbase/rtcm_bps/state"] == 0
    assert published["gnssbase/output_0_status/state"] == "Waiting"    # the caster, status index 1 -> 'W'
    assert published["gnssbase/output_1_status/state"] == "Connected"  # the raw log, index 2 -> 'C'
    assert published["gnssbase/str2str_diagnostics/state"] == "(1) recv error (111)"


def test_monitor_str2str_status_prints_fatal_startup_errors(capsys):
    """A fatal startup error (e.g. "device busy", wrong port) doesn't
    match the periodic status-line format and was previously silently
    dropped - found while diagnosing a real crash-loop where
    watchdog_str2str's generic "terminated unexpectedly, restarting..."
    was the only visible message, with no clue why. Real lines from a
    real str2str given a nonexistent port: "stream server start" /
    "stream server start error", both on stderr."""
    app = _bare_app()

    class FakeProc:
        args = ["str2str"]
        stderr = iter(["stream server start\n", "stream server start error\n"])

    fake_proc = FakeProc()
    app.str2str_proc = fake_proc
    app.monitor_str2str_status(fake_proc)

    out = capsys.readouterr().out
    assert "[str2str] stream server start" in out
    assert "[str2str] stream server start error" in out
    assert app.mqtt.published == []  # not a status line: no MQTT entity should be touched


@pytest.mark.skipif(shutil.which("str2str") is None, reason="requires the RTKLIB str2str binary in PATH")
def test_monitor_str2str_status_against_real_str2str_binary(fake_serial_pair, tmp_path):
    """Real (non-simulated) integration: runs the actual str2str with one
    deliberately unreachable output and one working one (file), and
    verifies that our parser correctly distinguishes the two states from
    the binary's real stderr lines."""
    master_fd, slave_path = fake_serial_pair
    bare_port = slave_path.removeprefix("/dev/")
    log_path = tmp_path / "log.rtcm3"

    # ntrip_casters has a single dummy entry: it's only there so that
    # output_stream_plan() generates two labels in the same order as the
    # two real -out targets passed below (tcpcli then file), to correctly
    # align the indices of the status string.
    app = _bare_app(
        ntrip_casters=[{"host": "x", "port": 1, "mountpoint": "M", "password": "p"}],
        rtcm_port=slave_path,
    )
    cmd = ["str2str", "-in", f"serial://{bare_port}:115200",
           "-out", "tcpcli://127.0.0.1:1", "-out", f"file://{log_path}"]
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
    app.str2str_proc = proc

    # stop_feeding prevents the writer thread from outliving the test: if
    # it kept writing to master_fd after the pty is closed, the file
    # descriptor number might already have been reassigned to a
    # subsequent test's pty, polluting it with unexpected RTCM bytes.
    stop_feeding = threading.Event()

    def feed():
        while not stop_feeding.is_set():
            try:
                os.write(master_fd, b"\xd3\x00\x13FAKE_RTCM_PAYLOAD_1234")
            except OSError:
                return
            time.sleep(0.3)

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    mon = threading.Thread(target=app.monitor_str2str_status, args=(proc,), daemon=True)
    mon.start()

    time.sleep(7)
    proc.terminate()
    proc.wait(timeout=5)
    stop_feeding.set()
    feeder.join(timeout=2)

    published = dict((t, p) for t, p in app.mqtt.published)
    # index 0 = the unreachable tcpcli caster -> never "Connected"
    assert published.get("gnssbase/output_0_status/state") in ("Closed", "Waiting", "Error")
    # index 1 = the log file, which instead writes data successfully
    assert published.get("gnssbase/output_1_status/state") == "Connected"
    assert log_path.exists() and log_path.stat().st_size > 0


def test_set_device_connected_publishes_only_on_state_change():
    app = _bare_app()
    app.set_device_connected(True)
    app.set_device_connected(True)  # no new "ON" event, only last_seen
    app.set_device_connected(False, reason="test")

    on_events = [p for t, p in app.mqtt.published if t.endswith("device_connected/state") and p == "ON"]
    off_events = [p for t, p in app.mqtt.published if t.endswith("device_connected/state") and p == "OFF"]
    assert len(on_events) == 1
    assert len(off_events) == 1


def test_configure_receiver_waits_then_succeeds_when_port_appears(fake_serial_pair, tmp_path):
    import serial_capture

    master_fd, slave_path = fake_serial_pair
    fake_port = str(tmp_path / "fake_ttyUSB0")

    app = _bare_app(rtcm_port=fake_port, nmea_port=fake_port)

    t = threading.Thread(target=app.configure_receiver, daemon=True)
    t.start()
    time.sleep(0.5)
    assert t.is_alive(), "must not terminate while the port does not exist"

    serial_capture.start_ascii_capture(master_fd)
    os.symlink(slave_path, fake_port)

    t.join(timeout=15)
    assert not t.is_alive()


def test_monitor_nmea_recovers_after_disconnect_different_ports(fake_serial_pair, tmp_path):
    """nmea_port != rtcm_port: monitor_nmea takes the direct-serial branch
    (_monitor_nmea_via_serial), unaffected by str2str."""
    master_fd, slave_path = fake_serial_pair
    fake_port = str(tmp_path / "fake_ttyUSB0")
    os.symlink(slave_path, fake_port)

    app = _bare_app(rtcm_port="/dev/null-fake-rtcm", nmea_port=fake_port)

    mon = threading.Thread(target=app.monitor_nmea, daemon=True)
    mon.start()
    time.sleep(0.3)
    os.write(master_fd, b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n")
    time.sleep(0.5)

    connected = [p for t, p in app.mqtt.published if t.endswith("device_connected/state")]
    assert connected and connected[-1] == "ON"

    os.close(master_fd)
    os.remove(fake_port)
    time.sleep(main.SERIAL_RETRY_INTERVAL_S + 2)

    connected = [p for t, p in app.mqtt.published if t.endswith("device_connected/state")]
    assert connected[-1] == "OFF"
    assert mon.is_alive(), "the monitor must not terminate: it must keep retrying"


def test_monitor_nmea_recovers_after_disconnect_same_port_via_relay(monkeypatch):
    """rtcm_port == nmea_port (the most common single-cable setup):
    monitor_nmea must read NMEA from the internal relay fed by str2str,
    not by opening the physical port a second time (see
    needs_internal_relay()/_monitor_nmea_via_relay's docstring - verified
    on real hardware that a second direct open there races with str2str
    and corrupts/loses data)."""
    monkeypatch.setattr(main, "SERIAL_SILENCE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(main, "RELAY_READ_TIMEOUT_S", 0.1)
    monkeypatch.setattr(main, "SERIAL_RETRY_INTERVAL_S", 0.1)

    app = _bare_app(rtcm_port="/dev/null-fake", nmea_port="/dev/null-fake")

    mon = threading.Thread(target=app.monitor_nmea, daemon=True)
    mon.start()

    for _ in range(50):
        if app.broadcaster.num_clients() >= 1:
            break
        time.sleep(0.05)
    assert app.broadcaster.num_clients() >= 1, "monitor_nmea must subscribe to the internal relay"

    # Simulates str2str forwarding a byte chunk from the receiver.
    app.broadcaster.broadcast(b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n")
    time.sleep(0.3)

    connected = [p for t, p in app.mqtt.published if t.endswith("device_connected/state")]
    assert connected and connected[-1] == "ON"

    # No more broadcasts: simulates str2str going quiet (e.g. receiver
    # disconnected) past the (patched) silence timeout.
    time.sleep(1.0)

    connected = [p for t, p in app.mqtt.published if t.endswith("device_connected/state")]
    assert connected[-1] == "OFF"
    assert mon.is_alive(), "the monitor must not terminate: it must keep retrying"


def test_save_position_backup_writes_file_and_publishes_mqtt(tmp_path, monkeypatch):
    monkeypatch.setattr(position_backup, "DEFAULT_PATH", tmp_path / "backup.json")
    app = _bare_app(receiver_type="unicore_um98x")

    app.save_position_backup(45.1, 9.2, 100.0, "survey_in", duration_sec=300, num_samples=42)

    saved = position_backup.load()
    assert saved["lat"] == 45.1
    assert saved["method"] == "survey_in"
    assert saved["num_samples"] == 42
    assert saved["receiver_type"] == "unicore_um98x"

    published = dict((t, p) for t, p in app.mqtt.published)
    assert published["gnssbase/position_backup/state"] == saved["computed_at"]
    import json
    assert json.loads(published["gnssbase/position_backup/attributes"]) == saved


def test_restore_position_backup_populates_manual_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(position_backup, "DEFAULT_PATH", tmp_path / "backup.json")
    position_backup.save(48.858, 2.294, 35.0, "ppp", "ublox_zedf9p", duration_hours=6)

    app = _bare_app(manual_lat=None, manual_lon=None, manual_height=None)
    app.restore_position_backup()

    assert app.manual_lat == 48.858
    assert app.manual_lon == 2.294
    assert app.manual_height == 35.0
    published = dict((t, p) for t, p in app.mqtt.published)
    assert published["gnssbase/manual_lat/state"] == "48.85800000"


def test_restore_position_backup_noop_when_no_backup_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(position_backup, "DEFAULT_PATH", tmp_path / "non_esiste.json")
    app = _bare_app(manual_lat=None, manual_lon=None, manual_height=None)
    app.restore_position_backup()
    assert app.manual_lat is None
    assert app.mqtt.published == []


def test_run_survey_in_reports_remaining_time_and_can_be_cancelled(fake_serial_pair):
    # rtcm_port == nmea_port: run_survey_in must read fixes via the
    # internal relay (see needs_internal_relay()), not a second direct
    # serial.Serial() on the same port already used by set_rover_mode
    # below (and, in production, continuously read by str2str). The pty
    # here only carries the "mode rover" command/response round-trip.
    master_fd, slave_path = fake_serial_pair
    app = _bare_app(rtcm_port=slave_path, nmea_port=slave_path, survey_duration=60)

    stop_feeding = threading.Event()

    def feed():
        while not stop_feeding.is_set():
            app.broadcaster.broadcast(b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n")
            time.sleep(0.1)

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()

    t = threading.Thread(target=app.run_survey_in, daemon=True)
    t.start()
    # set_rover_mode (a serial command) can block for up to ~1.5s waiting
    # for a response before the main loop starts (nothing writes one back
    # here: the feeder now feeds GGA through the relay, not the pty, since
    # this simulates the rtcm_port == nmea_port / relay-based reading
    # path): we wait beyond that margin to give time for the first
    # countdown publish.
    time.sleep(2.2)

    remaining_values = [p for k, p in app.mqtt.published if k.endswith("survey_in_remaining/state")]
    assert remaining_values, "must publish the remaining time while it's running"
    assert remaining_values[-1] <= 60

    app.cancel_survey_in()
    t.join(timeout=5)
    stop_feeding.set()
    feeder.join(timeout=2)

    assert not t.is_alive()
    assert not app.survey_running
    states = [p for k, p in app.mqtt.published if k.endswith("survey_in/state")]
    assert states[-1] == "cancelled"
    remaining_values = [p for k, p in app.mqtt.published if k.endswith("survey_in_remaining/state")]
    assert remaining_values[-1] == 0


def test_cancel_survey_in_is_noop_when_not_running():
    app = _bare_app(survey_running=False, survey_cancel_event=None)
    app.cancel_survey_in()  # must not raise or do anything
    assert app.mqtt.published == []


def test_run_ppp_campaign_reports_remaining_time_and_can_be_cancelled(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path))
    app = _bare_app(ppp_duration_hours=60 / 3600)  # 60 seconds, never reached: it'll be cancelled first

    t = threading.Thread(target=app.run_ppp_campaign, daemon=True)
    t.start()
    time.sleep(1.5)

    remaining_values = [p for k, p in app.mqtt.published if k.endswith("ppp_remaining/state")]
    assert remaining_values, "must publish the remaining time while it's running"
    assert 0 < remaining_values[-1] <= 60

    app.cancel_ppp_campaign()
    t.join(timeout=5)

    assert not t.is_alive()
    assert not app.ppp_running
    states = [p for k, p in app.mqtt.published if k.endswith("ppp_status/state")]
    assert states[-1] == "cancelled"
    remaining_values = [p for k, p in app.mqtt.published if k.endswith("ppp_remaining/state")]
    assert remaining_values[-1] == 0


def _mock_ppp_pipeline_up_to_products(monkeypatch, fetch_precise_products):
    """Mocks every ppp.* step except fetch_precise_products (the caller
    supplies that one, to control whether/when it succeeds)."""
    monkeypatch.setattr(ppp, "collect_raw_files", lambda *a, **k: ["fake.rtcm3"])
    monkeypatch.setattr(ppp, "concat_raw_files", lambda *a, **k: None)
    monkeypatch.setattr(ppp, "convbin", lambda *a, **k: ("obs", "nav"))
    monkeypatch.setattr(ppp, "parse_obs_dates", lambda *a, **k: [dt.date(2024, 1, 15)])
    monkeypatch.setattr(ppp, "fetch_precise_products", fetch_precise_products)
    monkeypatch.setattr(ppp, "run_rnx2rtkp", lambda *a, **k: "pos")
    monkeypatch.setattr(ppp, "parse_last_position", lambda *a, **k: (45.0, 9.0, 100.0))


def test_run_ppp_campaign_waits_for_products_then_completes(monkeypatch, tmp_path):
    """Regression: a real campaign used to fail outright ("error") the
    moment IGS products weren't published yet for such a recent date,
    instead of waiting - found from a real run on a user's Home Assistant
    instance. Simulates products becoming available on the third
    attempt."""
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(main, "PPP_PRODUCT_RETRY_INTERVAL_S", 0.2)
    monkeypatch.setattr(position_backup, "DEFAULT_PATH", tmp_path / "backup.json")

    attempts = []

    def fake_fetch(dates, workdir):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("not published yet")
        # FIN (the best tier) succeeding here means no background
        # refinement gets scheduled - kept out of scope for this test,
        # see test_run_ppp_campaign_schedules_refinement_after_rap_fix.
        return (["sp3"], ["clk"], "atx", {dt.date(2024, 1, 15): "FIN"})

    _mock_ppp_pipeline_up_to_products(monkeypatch, fake_fetch)

    app = _bare_app(ppp_duration_hours=1 / 3600, raw_log_retention_hours=72)
    monkeypatch.setattr(app.driver, "set_fixed_base", lambda *a, **k: None)

    app.run_ppp_campaign()

    assert len(attempts) == 3
    states = [p for k, p in app.mqtt.published if k.endswith("ppp_status/state")]
    assert "waiting_for_products" in states
    assert states[-1] == "done"
    assert app.manual_lat == 45.0
    refinement_states = [p for k, p in app.mqtt.published if k.endswith("ppp_refinement_status/state")]
    assert refinement_states[-1] == "idle"


def test_run_ppp_campaign_can_be_cancelled_while_waiting_for_products(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(main, "PPP_PRODUCT_RETRY_INTERVAL_S", 5)

    def always_fails(dates, workdir):
        raise RuntimeError("not published yet")

    _mock_ppp_pipeline_up_to_products(monkeypatch, always_fails)

    app = _bare_app(ppp_duration_hours=1 / 3600, raw_log_retention_hours=72)

    t = threading.Thread(target=app.run_ppp_campaign, daemon=True)
    t.start()
    # wait for the state to reach waiting_for_products before cancelling
    for _ in range(50):
        states = [p for k, p in app.mqtt.published if k.endswith("ppp_status/state")]
        if states and states[-1] == "waiting_for_products":
            break
        time.sleep(0.05)
    else:
        pytest.fail("campaign never reached waiting_for_products")

    app.cancel_ppp_campaign()
    t.join(timeout=5)

    assert not t.is_alive()
    assert not app.ppp_running
    states = [p for k, p in app.mqtt.published if k.endswith("ppp_status/state")]
    assert states[-1] == "cancelled"


def test_run_ppp_campaign_schedules_refinement_after_rap_fix(monkeypatch, tmp_path):
    """When the initial fix uses RAP (not the best available tier), the
    campaign must still complete ("done", applied to the receiver) rather
    than blocking on FIN for up to ~18 days - a separate background task
    then checks for FIN and, once found, exposes the refined position via
    the manual position fields WITHOUT reapplying it automatically (that
    remains a deliberate action, like every other manual position in this
    add-on)."""
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(main, "PPP_REFINEMENT_CHECK_INTERVAL_S", 0.2)
    monkeypatch.setattr(position_backup, "DEFAULT_PATH", tmp_path / "backup.json")

    def fake_fetch(dates, workdir, products=("FIN", "RAP")):
        if products == ("FIN",):
            return (["sp3f"], ["clkf"], "atxf", {dt.date(2024, 1, 15): "FIN"})
        return (["sp3"], ["clk"], "atx", {dt.date(2024, 1, 15): "RAP"})

    _mock_ppp_pipeline_up_to_products(monkeypatch, fake_fetch)
    # Overrides _mock_ppp_pipeline_up_to_products' own fixed-position stub:
    # must come after it to win, since this test needs two distinct
    # positions (RAP fix, then refined FIN fix) instead of one repeated.
    positions = iter([(45.0, 9.0, 100.0), (45.001, 9.001, 100.5)])
    monkeypatch.setattr(ppp, "parse_last_position", lambda *a, **k: next(positions))

    set_fixed_base_calls = []
    app = _bare_app(ppp_duration_hours=1 / 3600, raw_log_retention_hours=72)
    monkeypatch.setattr(app.driver, "set_fixed_base", lambda *a, **k: set_fixed_base_calls.append(a))

    app.run_ppp_campaign()

    assert len(set_fixed_base_calls) == 1  # the RAP-based fix was applied once
    assert app.manual_lat == 45.0
    states = [p for k, p in app.mqtt.published if k.endswith("ppp_status/state")]
    assert states[-1] == "done"

    for _ in range(50):
        refinement_states = [p for k, p in app.mqtt.published if k.endswith("ppp_refinement_status/state")]
        if refinement_states and refinement_states[-1] == "available":
            break
        time.sleep(0.05)
    else:
        pytest.fail("refinement never completed")

    assert app.manual_lat == 45.001, "the refined (FIN) position should replace the manual position fields"
    assert len(set_fixed_base_calls) == 1, "the refined position must NOT be applied automatically"


def test_ppp_refinement_stops_when_superseded_by_new_campaign(monkeypatch, tmp_path):
    """A new campaign starting bumps app.ppp_refinement_id, which must
    make any still-running run_ppp_refinement() from a previous campaign
    stop and clean up on its own instead of racing the new campaign for
    the same workdir."""
    monkeypatch.setattr(main, "PPP_REFINEMENT_CHECK_INTERVAL_S", 100)
    app = _bare_app(raw_log_retention_hours=72)
    app.ppp_refinement_id = 1

    workdir = tmp_path / "refinement_workdir"
    workdir.mkdir()

    t = threading.Thread(
        target=app.run_ppp_refinement,
        args=(1, [dt.date(2024, 1, 15)], workdir / "campaign.obs", workdir / "campaign.nav", workdir),
        daemon=True,
    )
    t.start()
    time.sleep(0.3)
    assert t.is_alive(), "must still be waiting for the next daily check"

    app.ppp_refinement_id = 2  # a new campaign started
    t.join(timeout=5)

    assert not t.is_alive()
    assert not workdir.exists(), "must clean up its workdir once superseded"


def test_resume_ppp_refinement_if_any_restarts_from_persisted_files(monkeypatch, tmp_path):
    """ppp_refinement_workdir is deliberately not wiped on add-on restart
    (unlike ppp_campaign_workdir) since the whole point of the daily FIN
    check is to survive until the next campaign starts, not until the
    next restart - this is what makes it actually resume."""
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path / "raw_logs"))
    workdir = tmp_path / "ppp_refinement_workdir"
    workdir.mkdir()
    (workdir / "campaign.obs").write_text(
        "     3.04           OBSERVATION DATA    M: Mixed            RINEX VERSION / TYPE\n"
        "  2024    01    15    00    00   00.0000000     GPS         TIME OF FIRST OBS   \n"
        "                                                            END OF HEADER       \n"
    )
    (workdir / "campaign.nav").write_text("dummy nav content\n")

    app = _bare_app(ppp_refinement_id=0)
    started = []
    monkeypatch.setattr(app, "run_ppp_refinement", lambda *a, **k: started.append(a))

    app._resume_ppp_refinement_if_any()
    time.sleep(0.1)

    assert app.ppp_refinement_id == 1
    assert len(started) == 1
    assert started[0][0] == 1  # refinement_id passed to the thread matches
    assert workdir.exists(), "must not delete the workdir it's about to resume"


def test_resume_ppp_refinement_if_any_discards_incomplete_workdir(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path / "raw_logs"))
    workdir = tmp_path / "ppp_refinement_workdir"
    workdir.mkdir()  # no campaign.obs/campaign.nav inside: nothing to resume from

    app = _bare_app(ppp_refinement_id=0)
    app._resume_ppp_refinement_if_any()

    assert app.ppp_refinement_id == 0, "nothing should have been resumed"
    assert not workdir.exists()


def test_run_ppp_campaign_keeps_retry_state_when_rnx2rtkp_fails(monkeypatch, tmp_path):
    """Regression: rnx2rtkp failing after IGS products were already
    downloaded (e.g. the real pos1-frequency/pos1-ionoopt config bug)
    used to delete the workdir outright, forcing a full re-log +
    re-download to try again. Now keeps everything so
    button.retry_ppp_computation can retry just this step."""
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path))

    def fake_fetch(dates, workdir, products=("FIN", "RAP")):
        return (["sp3"], ["clk"], "atx", {dt.date(2024, 1, 15): "RAP"})

    def fake_run_rnx2rtkp(*a, **k):
        raise RuntimeError("invalid option value pos1-frequency")

    _mock_ppp_pipeline_up_to_products(monkeypatch, fake_fetch)
    monkeypatch.setattr(ppp, "run_rnx2rtkp", fake_run_rnx2rtkp)

    app = _bare_app(ppp_duration_hours=1 / 3600, raw_log_retention_hours=72)
    app.run_ppp_campaign()

    states = [p for k, p in app.mqtt.published if k.endswith("ppp_status/state")]
    assert states[-1] == "error"
    assert not app.ppp_running
    assert app.ppp_retry_state is not None
    assert app.ppp_retry_state["tiers_used"] == {dt.date(2024, 1, 15): "RAP"}
    assert app.ppp_retry_state["workdir"].exists(), \
        "must keep the workdir (already-downloaded products) instead of deleting it"


def test_retry_ppp_computation_succeeds_with_preserved_state(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path / "raw_logs"))
    monkeypatch.setattr(position_backup, "DEFAULT_PATH", tmp_path / "backup.json")
    workdir = tmp_path / "ppp_campaign_workdir"
    workdir.mkdir()

    app = _bare_app(ppp_duration_hours=6, raw_log_retention_hours=72, ppp_running=False)
    monkeypatch.setattr(app.driver, "set_fixed_base", lambda *a, **k: None)
    app.ppp_retry_state = {
        "dates": [dt.date(2024, 1, 15)], "obs_path": "obs", "nav_path": "nav",
        "sp3_paths": ["sp3"], "clk_paths": ["clk"], "atx_path": "atx",
        "workdir": workdir, "raw_files": ["fake1.rtcm3", "fake2.rtcm3", "fake3.rtcm3"],
        "tiers_used": {dt.date(2024, 1, 15): "FIN"},
        "duration_hours": 6,
    }
    monkeypatch.setattr(ppp, "run_rnx2rtkp", lambda *a, **k: "pos")
    monkeypatch.setattr(ppp, "parse_last_position", lambda *a, **k: (45.0, 9.0, 100.0))

    app.retry_ppp_computation()

    assert app.ppp_retry_state is None, "resolved: nothing left to retry"
    assert app.manual_lat == 45.0
    states = [p for k, p in app.mqtt.published if k.endswith("ppp_status/state")]
    assert states[-1] == "done"
    assert not app.ppp_running


def test_retry_ppp_computation_is_noop_when_nothing_to_retry():
    app = _bare_app(ppp_retry_state=None, ppp_running=False)
    app.retry_ppp_computation()
    assert app.mqtt.published == []


def test_retry_ppp_computation_is_noop_while_a_campaign_is_running():
    app = _bare_app(ppp_retry_state={"anything": True}, ppp_running=True)
    app.retry_ppp_computation()
    assert app.mqtt.published == []
    assert app.ppp_retry_state is not None, "must not discard a valid retry state just because it's busy"


def test_run_ppp_campaign_clears_stale_retry_state_from_previous_attempt(monkeypatch, tmp_path):
    """Starting a fresh campaign wipes ppp_campaign_workdir regardless
    (existing behavior), so any pending retry_ppp_computation() state
    from an earlier failed attempt must be cleared too - otherwise the
    button would appear available but operate on now-deleted files."""
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path))
    app = _bare_app(ppp_duration_hours=60 / 3600, raw_log_retention_hours=72)
    app.ppp_retry_state = {"stale": True}

    t = threading.Thread(target=app.run_ppp_campaign, daemon=True)
    t.start()
    time.sleep(0.3)
    assert app.ppp_retry_state is None

    app.cancel_ppp_campaign()
    t.join(timeout=5)


def test_reprocess_existing_logs_uses_oldest_buffered_data_without_relogging(monkeypatch, tmp_path):
    """The whole point: reprocesses the OLDEST ppp_duration_hours of raw
    log still on disk, without ever going through a "logging" phase -
    older data is more likely to already have IGS products available
    (more real time has passed since it was observed) than "now", which
    is exactly as fresh, and exactly as bad a starting point for product
    availability, as a normal campaign's own freshly-logged data."""
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path / "raw_logs"))
    Path(main.RAW_LOG_DIR).mkdir(parents=True)
    monkeypatch.setattr(position_backup, "DEFAULT_PATH", tmp_path / "backup.json")

    now = time.time()
    oldest_file = Path(main.RAW_LOG_DIR) / "gnssbase_2024011500.rtcm3"
    oldest_file.write_bytes(b"old data")
    os.utime(oldest_file, (now - 40 * 3600, now - 40 * 3600))  # 40h old: within a 72h buffer
    newest_file = Path(main.RAW_LOG_DIR) / "gnssbase_2024011623.rtcm3"
    newest_file.write_bytes(b"new data")
    os.utime(newest_file, (now - 1, now - 1))  # essentially "now"

    collected_windows = []

    def fake_collect(raw_log_dir, start_ts, end_ts):
        collected_windows.append((start_ts, end_ts))
        return [str(oldest_file)]

    def fake_fetch(dates, workdir, products=("FIN", "RAP")):
        return (["sp3"], ["clk"], "atx", {dt.date(2024, 1, 15): "FIN"})

    monkeypatch.setattr(ppp, "collect_raw_files", fake_collect)
    monkeypatch.setattr(ppp, "concat_raw_files", lambda *a, **k: None)
    monkeypatch.setattr(ppp, "convbin", lambda *a, **k: ("obs", "nav"))
    monkeypatch.setattr(ppp, "parse_obs_dates", lambda *a, **k: [dt.date(2024, 1, 15)])
    monkeypatch.setattr(ppp, "fetch_precise_products", fake_fetch)
    monkeypatch.setattr(ppp, "run_rnx2rtkp", lambda *a, **k: "pos")
    monkeypatch.setattr(ppp, "parse_last_position", lambda *a, **k: (45.0, 9.0, 100.0))

    app = _bare_app(raw_log_retention_hours=72, ppp_duration_hours=6)
    monkeypatch.setattr(app.driver, "set_fixed_base", lambda *a, **k: None)

    app.reprocess_existing_logs()

    assert len(collected_windows) == 1
    start_ts, end_ts = collected_windows[0]
    assert start_ts == pytest.approx(now - 40 * 3600, abs=2), \
        "must start from the oldest file's timestamp, not from raw_log_retention_hours ago"
    assert (end_ts - start_ts) / 3600 == pytest.approx(6, rel=0.01), \
        "window must span ppp_duration_hours, not the whole buffer"
    states = [p for k, p in app.mqtt.published if k.endswith("ppp_status/state")]
    assert "logging" not in states, "must not go through a logging phase"
    assert states[-1] == "done"
    assert app.manual_lat == 45.0


def test_reprocess_existing_logs_errors_when_buffer_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path / "raw_logs"))  # doesn't exist: empty buffer
    app = _bare_app(raw_log_retention_hours=72, ppp_duration_hours=6)

    app.reprocess_existing_logs()

    states = [p for k, p in app.mqtt.published if k.endswith("ppp_status/state")]
    assert states[-1] == "error"
    assert not app.ppp_running


def test_clear_raw_log_buffer_removes_files_and_resets_buffer_hours(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path / "raw_logs"))
    Path(main.RAW_LOG_DIR).mkdir(parents=True)
    raw1 = Path(main.RAW_LOG_DIR) / "gnssbase_2024011500.rtcm3"
    raw1.write_bytes(b"data")

    app = _bare_app(raw_log_retention_hours=72)
    app.ppp_retry_state = {"stale": True}
    app.ppp_refinement_id = 1

    app.clear_raw_log_buffer()

    assert not raw1.exists()
    assert app.ppp_retry_state is None
    assert app.ppp_refinement_id == 2, "must supersede any pending refinement too"
    buffer_states = [p for k, p in app.mqtt.published if k.endswith("raw_log_buffer_hours/state")]
    assert buffer_states[-1] == 0


def test_clear_raw_log_buffer_is_noop_while_a_ppp_operation_is_running(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path / "raw_logs"))
    Path(main.RAW_LOG_DIR).mkdir(parents=True)
    raw1 = Path(main.RAW_LOG_DIR) / "gnssbase_2024011500.rtcm3"
    raw1.write_bytes(b"data")

    app = _bare_app(ppp_running=True)
    app.clear_raw_log_buffer()

    assert raw1.exists(), "must not delete data out from under an in-progress PPP operation"


def test_reprocess_existing_logs_is_noop_while_a_campaign_is_running():
    app = _bare_app(ppp_running=True)
    app.reprocess_existing_logs()
    assert app.mqtt.published == []


def test_reprocess_existing_logs_supersedes_pending_retry_state(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path))  # doesn't exist: empty buffer -> quick error exit

    app = _bare_app(raw_log_retention_hours=72)
    app.ppp_retry_state = {"stale": True}

    app.reprocess_existing_logs()

    assert app.ppp_retry_state is None
    states = [p for k, p in app.mqtt.published if k.endswith("ppp_status/state")]
    assert states[-1] == "error"


def test_archive_ppp_source_logs_copies_files_to_permanent_location(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path / "raw_logs"))
    Path(main.RAW_LOG_DIR).mkdir(parents=True)
    raw1 = Path(main.RAW_LOG_DIR) / "gnssbase_2024011500.rtcm3"
    raw2 = Path(main.RAW_LOG_DIR) / "gnssbase_2024011501.rtcm3"
    raw1.write_bytes(b"raw data 1")
    raw2.write_bytes(b"raw data 2")

    app = _bare_app()
    app._archive_ppp_source_logs([raw1, raw2], "2024-01-15T12:00:00+00:00")

    archive_dir = Path(main.RAW_LOG_DIR).parent / "ppp_source_logs" / "2024-01-15T12-00-00+00-00"
    assert (archive_dir / raw1.name).read_bytes() == b"raw data 1"
    assert (archive_dir / raw2.name).read_bytes() == b"raw data 2"


def test_archive_ppp_source_logs_survives_missing_files(monkeypatch, tmp_path, capsys):
    """A delayed retry_ppp_computation() can run long enough after the
    original failure that cleanup_raw_logs() has since rotated some of
    the original raw files out - archiving must not crash over that,
    just archive whatever's still there and warn about the rest."""
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path / "raw_logs"))
    app = _bare_app()
    app._archive_ppp_source_logs([tmp_path / "gone.rtcm3"], "2024-01-15T12:00:00+00:00")
    assert "could not archive" in capsys.readouterr().out


def test_archive_ppp_source_logs_survives_cleanup_raw_logs(monkeypatch, tmp_path):
    """The whole point: unlike RAW_LOG_DIR, the archive lives in a
    completely separate directory that cleanup_raw_logs() never globs,
    so it's untouched by raw_log_retention_hours-based deletion - the
    source data behind an applied position should never be silently
    deleted by routine log rotation."""
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path / "raw_logs"))
    Path(main.RAW_LOG_DIR).mkdir(parents=True)
    raw1 = Path(main.RAW_LOG_DIR) / "gnssbase_2024011500.rtcm3"
    raw1.write_bytes(b"raw data 1")

    app = _bare_app(raw_log_retention_hours=0)  # would delete anything in RAW_LOG_DIR immediately
    app._archive_ppp_source_logs([raw1], "2024-01-15T12:00:00+00:00")

    old = time.time() - 1000
    os.utime(raw1, (old, old))  # make it look stale to cleanup_raw_logs()
    t = threading.Thread(target=app.cleanup_raw_logs, daemon=True)
    t.start()
    time.sleep(0.3)

    assert not raw1.exists(), "sanity check: cleanup_raw_logs() did remove the original"
    archive_dir = Path(main.RAW_LOG_DIR).parent / "ppp_source_logs" / "2024-01-15T12-00-00+00-00"
    assert (archive_dir / raw1.name).exists(), "the archived copy must be untouched by cleanup"


def test_run_ppp_campaign_errors_once_retention_window_closes(monkeypatch, tmp_path):
    """If IGS products still aren't available by the time the raw log
    itself would already have been deleted (raw_log_retention_hours after
    the campaign ended), retrying further is pointless: gives up with a
    clear error instead of waiting forever."""
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(main, "PPP_PRODUCT_RETRY_INTERVAL_S", 0.2)

    def always_fails(dates, workdir):
        raise RuntimeError("not published yet")

    _mock_ppp_pipeline_up_to_products(monkeypatch, always_fails)

    # raw_log_retention_hours so small that the wait deadline is already
    # in the past by the time the first retry check runs.
    app = _bare_app(ppp_duration_hours=1 / 3600, raw_log_retention_hours=0)
    app.run_ppp_campaign()

    states = [p for k, p in app.mqtt.published if k.endswith("ppp_status/state")]
    assert states[-1] == "error"
    assert not app.ppp_running


def test_cancel_ppp_campaign_is_noop_when_not_running():
    app = _bare_app(ppp_running=False, ppp_cancel_event=None)
    app.cancel_ppp_campaign()
    assert app.mqtt.published == []
