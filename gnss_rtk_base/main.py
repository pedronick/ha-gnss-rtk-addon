#!/usr/bin/env python3
"""
RTK Base Station add-on: legge un ricevitore GNSS RTK via seriale (driver
selezionabile con receiver_type, vedi drivers/), spinge le correzioni RTCM
come server NTRIP (RTKLIB str2str) verso uno o più caster, ed espone
stato/controlli in Home Assistant via MQTT Discovery.
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

# Formato della riga di stato di str2str (verificato con un binario reale):
# "2024/01/15 12:34:56 [CC---]        425 B     699 bps (1) send error (111) "
STR2STR_STATUS_RE = re.compile(r"\[(?P<statuses>[^\]]*)\]\s+\d+\s+B\s+(?P<bps>-?\d+)\s+bps\s*(?P<msg>.*)$")
STR2STR_STATUS_LABELS = {"E": "Errore", "-": "Chiuso", "W": "In attesa", "C": "Connesso"}


def load_options():
    with open(OPTIONS_PATH) as f:
        return json.load(f)


def wait_for_serial_port(path, label):
    """Blocca finché il device seriale non esiste. Utile sia all'avvio
    (l'USB potrebbe non essere ancora enumerata) sia dopo uno scollegamento."""
    first = True
    while not os.path.exists(path):
        if first:
            print(f"[main] {label}: porta {path} non trovata, in attesa che compaia...", flush=True)
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
        status = "connesso" if connected else f"NON connesso ({reason})" if reason else "NON connesso"
        print(f"[main] stato dispositivo: {status}", flush=True)

    def configure_receiver(self):
        """Configura RTCM e NMEA sul modulo, aspettando che le porte
        seriali esistano (utile se l'USB non è ancora enumerata all'avvio
        dell'add-on) e ritentando in caso di errore invece di far crashare
        l'intero add-on."""
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
                print(f"[main] errore configurando il modulo ({e}), ritento in "
                      f"{SERIAL_RETRY_INTERVAL_S}s...", flush=True)
                time.sleep(SERIAL_RETRY_INTERVAL_S)

    def active_casters(self):
        return [c for c in self.ntrip_casters if c.get("host")]

    def output_stream_plan(self):
        """Etichette degli -out passati a str2str, nello stesso ordine in
        cui vengono aggiunti da build_str2str_cmd(). Servono per
        interpretare la stringa di stato a 5 caratteri che str2str stampa
        periodicamente su stderr: indice 0 = input seriale, indice i =
        l'i-esimo -out in questo stesso ordine (verificato leggendo
        strsvrstat()/str2str.c di RTKLIB e con un binario compilato)."""
        labels = [f"{c['host']}:{c['port']}/{c.get('mountpoint', '')}" for c in self.active_casters()]
        labels.append("log raw")
        if self.caster_enabled:
            labels.append("caster locale")
        return labels

    def validate_stream_budget(self):
        """RTKLIB limita str2str a MAXSTR=5 stream totali (1 input + 4
        output, verificato nel sorgente app/consapp/str2str/str2str.c):
        oltre il quarto -out, gli argomenti in eccesso vengono ignorati in
        modo silenzioso e imprevedibile, non con un errore chiaro. Meglio
        fallire rumorosamente qui che scoprirlo a runtime."""
        needed = len(self.output_stream_plan())
        if needed > 4:
            raise ValueError(
                f"Troppi output configurati per str2str: {needed} "
                f"({len(self.active_casters())} caster NTRIP + 1 log raw"
                f"{' + 1 caster locale' if self.caster_enabled else ''}), "
                f"il massimo supportato da RTKLIB è 4. Riduci il numero di "
                f"voci in ntrip_casters oppure disabilita caster_enabled."
            )

    def build_str2str_cmd(self):
        """Un'unica istanza str2str legge la seriale una sola volta e la
        smista su più -out (uno per caster + uno per il log raw continuo).
        Leggere la stessa porta seriale da più processi indipendenti
        corromperebbe lo stream: str2str supporta nativamente più -out
        proprio per questo caso d'uso (max 4, vedi validate_stream_budget)."""
        # str2str usa serial://<nome_device>, senza "/dev/": lo antepone lui
        # stesso internamente (openserial() in src/stream.c fa sprintf(dev,
        # "/dev/%s", port)). Passare "/dev/ttyUSB0" produrrebbe
        # "/dev//dev/ttyUSB0", che non esiste: str2str non si avvierebbe mai.
        # Verificato compilando e lanciando davvero str2str con un path
        # completo (fallisce) e con il nome nudo (funziona).
        str2str_port = self.rtcm_port.removeprefix("/dev/")
        cmd = ["str2str", "-in", f"serial://{str2str_port}:{self.baud}"]
        for caster_cfg in self.active_casters():
            # Sintassi RTKLIB per un output NTRIP server: "ntrips://[:passwd@]addr[:port]/mntpnt".
            # Non esiste un campo utente: str2str, nel ruolo di server/encoder,
            # invia solo la password del mountpoint (protocollo NTRIP1 "SOURCE
            # <password> <mountpoint>" — verificato in src/stream.c/reqntrip_s
            # di RTKLIB, che non usa mai lo username per questo ruolo).
            out = (f"ntrips://:{caster_cfg.get('password', '')}"
                   f"@{caster_cfg['host']}:{caster_cfg['port']}/{caster_cfg.get('mountpoint', '')}")
            cmd += ["-out", out]
        # Log raw continuo, con rotazione oraria automatica (str2str
        # riconosce i tag %Y%m%d%h nel path e crea un nuovo file ogni ora).
        cmd += ["-out", f"file://{RAW_LOG_DIR}/gnssbase_%Y%m%d%h.rtcm3"]
        if self.caster_enabled:
            # str2str si collega al nostro relay interno (caster.py) e ci
            # inoltra lo stream, che ridistribuiamo ai rover connessi.
            cmd += ["-out", f"tcpcli://127.0.0.1:{caster.INTERNAL_RELAY_PORT}"]
        return cmd

    def start_str2str(self):
        cmd = self.build_str2str_cmd()
        print("[main] avvio str2str:", " ".join(cmd), flush=True)
        self.str2str_proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self.monitor_str2str_status, args=(self.str2str_proc,), daemon=True).start()

    def watchdog_str2str(self):
        while True:
            time.sleep(10)
            if self.str2str_proc is not None and self.str2str_proc.poll() is not None:
                print("[main] str2str terminato inaspettatamente, riavvio...", flush=True)
                self.str2str_proc = subprocess.Popen(
                    self.str2str_proc.args, stderr=subprocess.PIPE, text=True, bufsize=1)
                threading.Thread(target=self.monitor_str2str_status, args=(self.str2str_proc,), daemon=True).start()

    def monitor_str2str_status(self, proc):
        """Legge lo stderr di str2str (stampa una riga di stato ogni 5s di
        default: '<data/ora> [<5 caratteri di stato>] <byte> B <bps> bps
        <messaggi per stream>') e la traduce in entità MQTT per-output.
        Formato verificato compilando e lanciando davvero str2str, non
        dedotto dal solo help text."""
        labels = self.output_stream_plan()
        for line in proc.stderr:
            if proc is not self.str2str_proc:
                return  # e' stato riavviato dal watchdog: questo thread e' obsoleto
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
        """Cancella periodicamente i file di log raw più vecchi della
        retention configurata, per non riempire il disco (il logging è
        sempre attivo per poter avviare una campagna PPP in qualsiasi
        momento su una finestra recente)."""
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
            "satellites", "Satelliti in uso", f"{BASE}/satellites/state", unit="satelliti"))
        d.publish_discovery(self.mqtt, "sensor", "accuracy", d.sensor_config(
            "accuracy", "Accuratezza stimata", f"{BASE}/accuracy/state", unit="m"))
        d.publish_discovery(self.mqtt, "sensor", "survey_in_status", d.sensor_config(
            "survey_in_status", "Survey-In", f"{BASE}/survey_in/state"))
        d.publish_discovery(self.mqtt, "button", "survey_in_start", d.button_config(
            "survey_in_start", "Avvia Survey-In", f"{BASE}/survey_in/set", icon="mdi:crosshairs"))
        d.publish_discovery(self.mqtt, "button", "survey_in_cancel", d.button_config(
            "survey_in_cancel", "Annulla Survey-In", f"{BASE}/survey_in_cancel/set", icon="mdi:cancel"))
        d.publish_discovery(self.mqtt, "sensor", "survey_in_remaining", d.sensor_config(
            "survey_in_remaining", "Survey-In: tempo rimanente", f"{BASE}/survey_in_remaining/state",
            unit="s", icon="mdi:timer-sand", device_class="duration"))
        d.publish_discovery(self.mqtt, "number", "manual_lat", d.number_config(
            "manual_lat", "Latitudine manuale", f"{BASE}/manual_lat/state",
            f"{BASE}/manual_lat/set", -90, 90, 0.0000001, "°"))
        d.publish_discovery(self.mqtt, "number", "manual_lon", d.number_config(
            "manual_lon", "Longitudine manuale", f"{BASE}/manual_lon/state",
            f"{BASE}/manual_lon/set", -180, 180, 0.0000001, "°"))
        d.publish_discovery(self.mqtt, "number", "manual_height", d.number_config(
            "manual_height", "Altezza manuale", f"{BASE}/manual_height/state",
            f"{BASE}/manual_height/set", -500, 9000, 0.001, "m"))
        d.publish_discovery(self.mqtt, "button", "apply_manual_position", d.button_config(
            "apply_manual_position", "Applica posizione manuale",
            f"{BASE}/apply_manual_position/set", icon="mdi:map-marker-check"))
        d.publish_discovery(self.mqtt, "sensor", "ppp_status", d.sensor_config(
            "ppp_status", "Campagna PPP", f"{BASE}/ppp_status/state", icon="mdi:satellite-variant"))
        d.publish_discovery(self.mqtt, "number", "ppp_duration_hours", d.number_config(
            "ppp_duration_hours", "Durata campagna PPP", f"{BASE}/ppp_duration_hours/state",
            f"{BASE}/ppp_duration_hours/set", 1, 48, 1, "h"))
        d.publish_discovery(self.mqtt, "button", "ppp_start", d.button_config(
            "ppp_start", "Avvia campagna PPP", f"{BASE}/ppp_start/set", icon="mdi:satellite-uplink"))
        d.publish_discovery(self.mqtt, "button", "ppp_cancel", d.button_config(
            "ppp_cancel", "Annulla campagna PPP", f"{BASE}/ppp_cancel/set", icon="mdi:cancel"))
        d.publish_discovery(self.mqtt, "sensor", "ppp_remaining", d.sensor_config(
            "ppp_remaining", "Campagna PPP: tempo rimanente", f"{BASE}/ppp_remaining/state",
            unit="s", icon="mdi:timer-sand", device_class="duration"))
        if self.caster_enabled:
            d.publish_discovery(self.mqtt, "sensor", "caster_clients", d.sensor_config(
                "caster_clients", "Rover connessi (caster locale)",
                f"{BASE}/caster_clients/state", unit="rover", icon="mdi:radio-tower"))
        d.publish_discovery(self.mqtt, "binary_sensor", "device_connected", d.binary_sensor_config(
            "device_connected", "Dispositivo connesso", f"{BASE}/device_connected/state",
            device_class="connectivity"))
        d.publish_discovery(self.mqtt, "sensor", "last_seen", d.sensor_config(
            "last_seen", "Ultimo dato ricevuto", f"{BASE}/last_seen/state",
            icon="mdi:clock-outline", device_class="timestamp"))
        d.publish_discovery(self.mqtt, "sensor", "config_error", d.sensor_config(
            "config_error", "Errore di configurazione", f"{BASE}/config_error/state",
            icon="mdi:alert-circle-outline"))
        d.publish_discovery(self.mqtt, "sensor", "position_backup", d.sensor_config(
            "position_backup", "Ultima posizione calcolata", f"{BASE}/position_backup/state",
            icon="mdi:map-marker-check-outline", device_class="timestamp",
            json_attributes_topic=f"{BASE}/position_backup/attributes"))
        d.publish_discovery(self.mqtt, "sensor", "rtcm_bps", d.sensor_config(
            "rtcm_bps", "RTCM bitrate in ingresso", f"{BASE}/rtcm_bps/state",
            unit="bps", icon="mdi:speedometer"))
        d.publish_discovery(self.mqtt, "sensor", "str2str_diagnostics", d.sensor_config(
            "str2str_diagnostics", "Diagnostica str2str", f"{BASE}/str2str_diagnostics/state",
            icon="mdi:text-box-outline"))
        for i, label in enumerate(self.output_stream_plan()):
            d.publish_discovery(self.mqtt, "sensor", f"output_{i}_status", d.sensor_config(
                f"output_{i}_status", f"Stato uscita {i + 1}: {label}",
                f"{BASE}/output_{i}_status/state", icon="mdi:transmission-tower"))

    def on_connect(self, client, userdata, flags, rc):
        print("[mqtt] connesso, rc=", rc, flush=True)
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
        """All'avvio, ripristina in memoria l'ultima posizione calcolata
        (se presente su disco) e la esibisce come 'posizione manuale' e
        come sensore di backup — utile se il container viene ricreato: il
        ricevitore potrebbe aver già la sua config salvata, ma l'add-on da
        solo non ricorderebbe altrimenti nulla su come/quando è stata
        calcolata. Non la rimanda al ricevitore: solo dati/visibilità,
        l'applicazione resta un'azione deliberata (pulsante)."""
        data = position_backup.load()
        if not data:
            return
        self.manual_lat, self.manual_lon, self.manual_height = data["lat"], data["lon"], data["height"]
        self.mqtt.publish(f"{BASE}/manual_lat/state", f"{data['lat']:.8f}", retain=True)
        self.mqtt.publish(f"{BASE}/manual_lon/state", f"{data['lon']:.8f}", retain=True)
        self.mqtt.publish(f"{BASE}/manual_height/state", f"{data['height']:.3f}", retain=True)
        self.mqtt.publish(f"{BASE}/position_backup/state", data["computed_at"], retain=True)
        self.mqtt.publish(f"{BASE}/position_backup/attributes", json.dumps(data), retain=True)
        print(f"[main] posizione di backup ripristinata: {data['method']} @ {data['computed_at']}", flush=True)

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
            print("[survey-in] richiesta di annullamento ricevuta", flush=True)
            self.survey_cancel_event.set()

    def cancel_ppp_campaign(self):
        if self.ppp_running and self.ppp_cancel_event:
            print("[ppp] richiesta di annullamento ricevuta", flush=True)
            self.ppp_cancel_event.set()

    # ------------------------------------------------------------ survey-in

    def save_position_backup(self, lat, lon, height, method, **extra):
        """Salva su /data la posizione appena fissata, con provenienza
        (metodo, data/ora, parametri), e pubblica lo stesso contenuto come
        entità MQTT (stato = timestamp, attributi = resto dei dati)."""
        data = position_backup.save(lat, lon, height, method, self.receiver_type, **extra)
        self.mqtt.publish(f"{BASE}/position_backup/state", data["computed_at"], retain=True)
        self.mqtt.publish(f"{BASE}/position_backup/attributes", json.dumps(data), retain=True)

    def run_survey_in(self):
        """Media di posizioni standalone (single-point) per un tempo breve.

        ATTENZIONE: questo è un survey-in "rapido" in stile consumer
        (accuratezza tipica metrica/sub-metrica), NON equivalente al
        posizionamento PPP statico di più ore usato per determinare la
        posizione di installazione definitiva. Usalo per una stima veloce
        o per ricollocazioni minori, non come sostituto del PPP.
        """
        if self.survey_running:
            return
        self.survey_running = True
        self.survey_cancel_event = threading.Event()
        self.mqtt.publish(f"{BASE}/survey_in/state", "running", retain=True)
        print(f"[survey-in] avviato per {self.survey_duration}s", flush=True)

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
            print("[survey-in] errore:", e, flush=True)
            self.mqtt.publish(f"{BASE}/survey_in/state", "error", retain=True)
            self.mqtt.publish(f"{BASE}/survey_in_remaining/state", 0, retain=True)
            self.survey_running = False
            return

        self.mqtt.publish(f"{BASE}/survey_in_remaining/state", 0, retain=True)

        if cancelled:
            print(f"[survey-in] annullato dall'utente dopo {len(samples)} campioni", flush=True)
            self.mqtt.publish(f"{BASE}/survey_in/state", "cancelled", retain=True)
            self.survey_running = False
            return

        if len(samples) < 10:
            print("[survey-in] fallito: troppi pochi fix raccolti", flush=True)
            self.mqtt.publish(f"{BASE}/survey_in/state", "error", retain=True)
            self.survey_running = False
            return

        avg_lat = statistics.mean(s[0] for s in samples)
        avg_lon = statistics.mean(s[1] for s in samples)
        heights = [s[2] for s in samples if s[2] is not None]
        avg_height = statistics.mean(heights) if heights else 0.0

        self.driver.set_fixed_base(self.rtcm_port, self.baud, avg_lat, avg_lon, avg_height)
        print(f"[survey-in] completato: {avg_lat:.7f}, {avg_lon:.7f}, {avg_height:.2f} "
              f"({len(samples)} campioni)", flush=True)
        self.save_position_backup(avg_lat, avg_lon, avg_height, "survey_in",
                                   duration_sec=self.survey_duration, num_samples=len(samples))
        self.mqtt.publish(f"{BASE}/survey_in/state", "done", retain=True)
        self.survey_running = False

    # -------------------------------------------------------------- PPP

    def run_ppp_campaign(self):
        """Campagna PPP-static: aspetta ppp_duration_hours ore accumulando
        il log raw (già sempre attivo via str2str), poi elabora la finestra
        con RTKLIB (convbin + rnx2rtkp PPP-static + prodotti IGS) e applica
        la posizione risultante come base fissa. Precisione attesa:
        centimetrica con log di diverse ore, a differenza del survey-in
        rapido che è solo metrico."""
        if self.ppp_running:
            return
        self.ppp_running = True
        self.ppp_cancel_event = threading.Event()
        start_ts = time.time()
        duration_s = self.ppp_duration_hours * 3600
        deadline = start_ts + duration_s
        self.mqtt.publish(f"{BASE}/ppp_status/state", "logging", retain=True)
        print(f"[ppp] campagna avviata, durata {self.ppp_duration_hours}h", flush=True)

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if self.ppp_cancel_event.is_set():
                print("[ppp] campagna annullata dall'utente durante la registrazione", flush=True)
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
                    raise RuntimeError("Nessun file di log raw trovato per la finestra della campagna")
                raw_concat = workdir / "campaign.rtcm3"
                ppp.concat_raw_files(raw_files, raw_concat)

                obs_path, nav_path = ppp.convbin(str(raw_concat), str(workdir))
                dates = ppp.parse_obs_dates(obs_path)
                sp3_paths, clk_paths, atx_path = ppp.fetch_precise_products(dates, workdir)
                pos_path = ppp.run_rnx2rtkp(obs_path, nav_path, sp3_paths, clk_paths, atx_path, workdir)
                lat, lon, height = ppp.parse_last_position(pos_path)
        except Exception as e:
            print("[ppp] errore:", e, flush=True)
            self.mqtt.publish(f"{BASE}/ppp_status/state", "error", retain=True)
            self.ppp_running = False
            return

        self.driver.set_fixed_base(self.rtcm_port, self.baud, lat, lon, height)
        print(f"[ppp] completato: {lat:.8f}, {lon:.8f}, {height:.3f}", flush=True)
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
            print("[main] posizione manuale incompleta: imposta lat/lon/height prima di applicare", flush=True)
            return
        self.driver.set_fixed_base(self.rtcm_port, self.baud, self.manual_lat, self.manual_lon, self.manual_height)
        print("[main] posizione manuale applicata:",
              self.manual_lat, self.manual_lon, self.manual_height, flush=True)
        self.save_position_backup(self.manual_lat, self.manual_lon, self.manual_height, "manual")

    # -------------------------------------------------------------- monitor

    def monitor_nmea(self):
        """Loop principale di lettura NMEA, con riconnessione automatica:
        se la porta seriale scompare (USB scollegata) o non arriva più
        nulla per SERIAL_SILENCE_TIMEOUT_S secondi, segnala la
        disconnessione su MQTT e ritenta ad intervalli regolari, senza
        mai far terminare il processo dell'add-on."""
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
                                raise serial.SerialException("la porta seriale non esiste più")
                            if time.time() - last_data_ts > SERIAL_SILENCE_TIMEOUT_S:
                                raise serial.SerialException(
                                    f"nessun dato da oltre {SERIAL_SILENCE_TIMEOUT_S}s")
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
        print(f"[main] driver ricevitore: {self.receiver_type} "
              f"({getattr(self.driver, 'NAME', '?')})", flush=True)
        # MQTT va connesso prima di configure_receiver(): quest'ultima può
        # dover attendere a lungo che l'USB compaia, e in quell'attesa
        # vogliamo poter pubblicare lo stato "non connesso" invece di
        # restare in silenzio.
        mqtt_host = os.environ.get("MQTT_HOST", "localhost")
        mqtt_port = int(os.environ.get("MQTT_PORT", 1883))
        while True:
            try:
                self.mqtt.connect(mqtt_host, mqtt_port, keepalive=30)
                break
            except OSError as e:
                print(f"[main] broker MQTT non raggiungibile ({mqtt_host}:{mqtt_port}): {e}. "
                      f"Installa/avvia l'add-on Mosquitto broker (o configura un broker esterno). "
                      f"Ritento in {MQTT_RETRY_INTERVAL_S}s...", flush=True)
                time.sleep(MQTT_RETRY_INTERVAL_S)
        self.mqtt.loop_start()

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
        threading.Thread(target=self.cleanup_raw_logs, daemon=True).start()
        threading.Thread(target=start_webserver, args=(self.state, nmea.fix_label, WEBUI_PORT), daemon=True).start()

        if self.caster_enabled:
            threading.Thread(target=caster.run_relay_receiver, args=(self.broadcaster,), daemon=True).start()
            threading.Thread(target=caster.run_caster_server,
                              args=(self.broadcaster, self.caster_mountpoint, self.caster_user,
                                    self.caster_password, caster.CASTER_PORT, self.caster_max_clients),
                              daemon=True).start()
            threading.Thread(target=self.publish_caster_clients, daemon=True).start()

        self.monitor_nmea()  # loop principale, bloccante


if __name__ == "__main__":
    App(load_options()).run()
