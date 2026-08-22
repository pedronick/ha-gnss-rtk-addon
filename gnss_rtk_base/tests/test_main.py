import os
import shutil
import subprocess
import threading
import time

import pytest

import caster
import drivers
import main
import position_backup
from state import SharedState


class FakeMqtt:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload))

    def username_pw_set(self, *a, **k):
        pass


def _bare_app(**overrides):
    """Costruisce un'istanza di App bypassando __init__ (che aprirebbe una
    vera connessione MQTT), impostando solo gli attributi necessari per i
    test della logica pura (build_str2str_cmd, resilienza seriale, ecc.)."""
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
    )
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(app, key, value)
    return app


def test_build_str2str_cmd_includes_one_out_per_caster_and_log_file():
    app = _bare_app(ntrip_casters=[
        {"host": "rtk2go.com", "port": 2101, "mountpoint": "A", "password": "p1"},
        {"host": "caster.example.com", "port": 2101, "mountpoint": "B", "password": "p2"},
        {"host": "", "port": 2101, "mountpoint": "", "password": ""},  # slot vuoto
    ])
    cmd = app.build_str2str_cmd()

    # str2str vuole il nome del device senza "/dev/" (lui lo prepone da
    # solo internamente: passare il path completo lo raddoppierebbe e
    # str2str non si avvierebbe mai). Verificato con un binario reale.
    assert cmd[:3] == ["str2str", "-in", "serial://null-fake:115200"]
    outs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-out"]
    assert len(outs) == 3  # 2 caster attivi + 1 file di log, lo slot vuoto è ignorato
    assert any("rtk2go.com" in o for o in outs)
    assert any("caster.example.com" in o for o in outs)
    assert any(o.startswith("file://") for o in outs)


def test_build_str2str_cmd_ntrips_url_has_no_username():
    """RTKLIB (reqntrip_s in stream.c) usa solo la password del mountpoint
    per il ruolo server/encoder: uno username nell'URL sarebbe silenziosamente
    ignorato. La sintassi corretta è ntrips://:password@host:port/mountpoint."""
    app = _bare_app(ntrip_casters=[
        {"host": "rtk2go.com", "port": 2101, "mountpoint": "MYMOUNT", "password": "secret"},
    ])
    cmd = app.build_str2str_cmd()
    outs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-out"]
    ntrip_out = next(o for o in outs if o.startswith("ntrips://"))
    assert ntrip_out == "ntrips://:secret@rtk2go.com:2101/MYMOUNT"


def test_build_str2str_cmd_does_not_shadow_caster_module():
    """Regressione: build_str2str_cmd usava 'caster' come nome del
    ciclo for, oscurando il modulo 'caster' importato — con
    caster_enabled=True e almeno un caster in lista, la riga che
    referenzia caster.INTERNAL_RELAY_PORT falliva con AttributeError."""
    app = _bare_app(
        ntrip_casters=[{"host": "rtk2go.com", "port": 2101, "mountpoint": "A", "password": "p"}],
        caster_enabled=True,
    )
    cmd = app.build_str2str_cmd()
    outs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-out"]
    relay_outs = [o for o in outs if o.startswith("tcpcli://127.0.0.1:")]
    assert len(relay_outs) == 1
    assert relay_outs[0] == f"tcpcli://127.0.0.1:{caster.INTERNAL_RELAY_PORT}"


def test_build_str2str_cmd_without_any_caster_configured():
    app = _bare_app(ntrip_casters=[])
    cmd = app.build_str2str_cmd()
    outs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-out"]
    assert len(outs) == 1  # solo il file di log
    assert outs[0].startswith("file://")


@pytest.mark.parametrize("port", ["/dev/ttyUSB0", "/dev/ttyACM0"])
def test_build_str2str_cmd_strips_dev_prefix_from_serial_port(port):
    """str2str prepone da solo '/dev/' al nome del device (openserial() in
    src/stream.c): passargli il path completo produrrebbe '/dev//dev/...',
    che non esiste, e str2str non partirebbe mai. Verificato con un
    binario reale (v2.4.3-b34) durante lo sviluppo."""
    app = _bare_app(rtcm_port=port)
    cmd = app.build_str2str_cmd()
    assert cmd[2] == f"serial://{port.removeprefix('/dev/')}:115200"
    assert "/dev/" not in cmd[2]


def test_validate_stream_budget_ok_at_the_limit():
    # 3 caster + 1 log = 4, esattamente al limite di RTKLIB (MAXSTR=5: 1 in + 4 out).
    app = _bare_app(ntrip_casters=[
        {"host": f"caster{i}.example.com", "port": 2101, "mountpoint": "M", "password": "p"}
        for i in range(3)
    ], caster_enabled=False)
    app.validate_stream_budget()  # non deve sollevare


def test_validate_stream_budget_raises_when_exceeded():
    # 3 caster + 1 log + 1 caster locale = 5, supera il limite di 4 -out.
    app = _bare_app(ntrip_casters=[
        {"host": f"caster{i}.example.com", "port": 2101, "mountpoint": "M", "password": "p"}
        for i in range(3)
    ], caster_enabled=True)
    with pytest.raises(ValueError, match="Troppi output"):
        app.validate_stream_budget()


def test_output_stream_plan_order_matches_build_str2str_cmd():
    app = _bare_app(
        ntrip_casters=[{"host": "rtk2go.com", "port": 2101, "mountpoint": "M1", "password": "p"}],
        caster_enabled=True,
    )
    plan = app.output_stream_plan()
    assert plan == ["rtk2go.com:2101/M1", "log raw", "caster locale"]


def test_monitor_str2str_status_parses_real_status_line_format():
    """Riga reale catturata lanciando davvero str2str v2.4.3-b34 con un
    output raggiungibile (file) e uno irraggiungibile (tcpcli):
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
    assert published["gnssbase/output_0_status/state"] == "In attesa"  # il caster, indice 1 dello stato -> 'W'
    assert published["gnssbase/output_1_status/state"] == "Connesso"   # il log raw, indice 2 -> 'C'
    assert published["gnssbase/str2str_diagnostics/state"] == "(1) recv error (111)"


@pytest.mark.skipif(shutil.which("str2str") is None, reason="richiede il binario str2str di RTKLIB nel PATH")
def test_monitor_str2str_status_against_real_str2str_binary(fake_serial_pair, tmp_path):
    """Integrazione reale (non simulata): lancia il vero str2str con un
    output volutamente irraggiungibile e uno funzionante (file), e
    verifica che il nostro parser distingua correttamente i due stati
    dalle righe di stderr reali del binario."""
    master_fd, slave_path = fake_serial_pair
    bare_port = slave_path.removeprefix("/dev/")
    log_path = tmp_path / "log.rtcm3"

    # ntrip_casters ha una sola voce fittizia: serve solo perché
    # output_stream_plan() generi due etichette nello stesso ordine dei
    # due -out reali passati sotto (tcpcli poi file), per allineare
    # correttamente gli indici della stringa di stato.
    app = _bare_app(
        ntrip_casters=[{"host": "x", "port": 1, "mountpoint": "M", "password": "p"}],
        rtcm_port=slave_path,
    )
    cmd = ["str2str", "-in", f"serial://{bare_port}:115200",
           "-out", "tcpcli://127.0.0.1:1", "-out", f"file://{log_path}"]
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
    app.str2str_proc = proc

    # stop_feeding evita che il thread scrittore sopravviva al test: se
    # continuasse a scrivere su master_fd dopo la chiusura della pty, il
    # numero di file descriptor potrebbe essere già stato riassegnato a
    # una pty di un test successivo, inquinandolo con byte RTCM inattesi.
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
    # indice 0 = il caster tcpcli irraggiungibile -> mai "Connesso"
    assert published.get("gnssbase/output_0_status/state") in ("Chiuso", "In attesa", "Errore")
    # indice 1 = il file di log, che invece scrive dati con successo
    assert published.get("gnssbase/output_1_status/state") == "Connesso"
    assert log_path.exists() and log_path.stat().st_size > 0


def test_set_device_connected_publishes_only_on_state_change():
    app = _bare_app()
    app.set_device_connected(True)
    app.set_device_connected(True)  # nessun nuovo evento "ON", solo last_seen
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
    assert t.is_alive(), "non deve terminare mentre la porta non esiste"

    serial_capture.start_ascii_capture(master_fd)
    os.symlink(slave_path, fake_port)

    t.join(timeout=15)
    assert not t.is_alive()


def test_monitor_nmea_recovers_after_disconnect(fake_serial_pair, tmp_path):
    master_fd, slave_path = fake_serial_pair
    fake_port = str(tmp_path / "fake_ttyUSB0")
    os.symlink(slave_path, fake_port)

    app = _bare_app(rtcm_port=fake_port, nmea_port=fake_port)

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
    assert mon.is_alive(), "il monitor non deve terminare: deve continuare a ritentare"


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
    master_fd, slave_path = fake_serial_pair
    app = _bare_app(rtcm_port=slave_path, nmea_port=slave_path, survey_duration=60)

    stop_feeding = threading.Event()

    def feed():
        while not stop_feeding.is_set():
            try:
                os.write(master_fd, b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n")
            except OSError:
                return
            time.sleep(0.1)

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()

    t = threading.Thread(target=app.run_survey_in, daemon=True)
    t.start()
    # set_rover_mode (comando seriale) puo' bloccarsi fino a ~1s in attesa
    # di una risposta prima che il loop principale parta: si attende oltre
    # quel margine per dare tempo alla prima pubblicazione del countdown.
    time.sleep(1.5)

    remaining_values = [p for k, p in app.mqtt.published if k.endswith("survey_in_remaining/state")]
    assert remaining_values, "deve pubblicare il tempo rimanente mentre e' in corso"
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
    app.cancel_survey_in()  # non deve sollevare né fare nulla
    assert app.mqtt.published == []


def test_run_ppp_campaign_reports_remaining_time_and_can_be_cancelled(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "RAW_LOG_DIR", str(tmp_path))
    app = _bare_app(ppp_duration_hours=60 / 3600)  # 60 secondi, mai raggiunti: verrà annullata prima

    t = threading.Thread(target=app.run_ppp_campaign, daemon=True)
    t.start()
    time.sleep(1.5)

    remaining_values = [p for k, p in app.mqtt.published if k.endswith("ppp_remaining/state")]
    assert remaining_values, "deve pubblicare il tempo rimanente mentre e' in corso"
    assert 0 < remaining_values[-1] <= 60

    app.cancel_ppp_campaign()
    t.join(timeout=5)

    assert not t.is_alive()
    assert not app.ppp_running
    states = [p for k, p in app.mqtt.published if k.endswith("ppp_status/state")]
    assert states[-1] == "cancelled"
    remaining_values = [p for k, p in app.mqtt.published if k.endswith("ppp_remaining/state")]
    assert remaining_values[-1] == 0


def test_cancel_ppp_campaign_is_noop_when_not_running():
    app = _bare_app(ppp_running=False, ppp_cancel_event=None)
    app.cancel_ppp_campaign()
    assert app.mqtt.published == []
