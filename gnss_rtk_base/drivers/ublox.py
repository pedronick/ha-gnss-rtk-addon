"""
Driver u-blox (protocollo binario UBX), per moduli come ZED-F9P/F9R/M8P.

Usa i messaggi UBX-CFG-MSG (classe 0x06 id 0x01) per abilitare RTCM3/NMEA
sulla porta corrente, e UBX-CFG-TMODE3 (0x06 0x71) per la modalità
rover/base fissa con coordinate LLA — sono i messaggi "legacy" del
protocollo UBX, supportati anche dai moduli più recenti (F9P) in aggiunta
a CFG-VALSET, quindi dovrebbero funzionare su tutta la famiglia M8P/F9P/F9R.

ATTENZIONE: gli ID dei messaggi RTCM3/NMEA e il layout del payload di
TMODE3 sono presi dalla documentazione pubblica u-blox (Interface
Description) al momento della scrittura, ma vanno verificati contro il
manuale specifico del tuo modulo/firmware prima dell'uso in produzione.
Ogni comando attende ora la risposta UBX-ACK-ACK/NAK (classe 0x05) e la
logga esplicitamente: un log "NAK" o "nessuna risposta" è un segnale
affidabile che qualcosa nel comando non è stato accettato dal modulo, da
verificare con il tuo Interface Manual o con u-center. Contratto del
driver: vedi drivers/base.py.
"""

import struct
import time

import serial

NAME = "u-blox ZED-F9P / M8P (protocollo UBX)"

UBX_SYNC1, UBX_SYNC2 = 0xB5, 0x62
CFG_MSG = (0x06, 0x01)
CFG_TMODE3 = (0x06, 0x71)
ACK_CLASS = 0x05
ACK_ACK, ACK_NAK = 0x01, 0x00

# ID dei messaggi RTCM3 nella classe UBX 0xF5.
RTCM_MSG_IDS = {
    1005: 0x05,
    1077: 0x4D,
    1087: 0x4F,
    1097: 0x61,
    1127: 0x7F,
}
# ID dei messaggi NMEA standard nella classe UBX 0xF0.
NMEA_MSG_IDS = {
    "GGA": 0x00,
    "GSA": 0x02,
    "GSV": 0x03,
    "GST": 0x07,
}


def _checksum(data):
    ck_a = ck_b = 0
    for b in data:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return ck_a, ck_b


def _frame(msg_class, msg_id, payload):
    body = bytes([msg_class, msg_id]) + struct.pack("<H", len(payload)) + payload
    ck_a, ck_b = _checksum(body)
    return bytes([UBX_SYNC1, UBX_SYNC2]) + body + bytes([ck_a, ck_b])


def _read_ack(ser, msg_class, msg_id, timeout=1.0):
    """Cerca nel flusso in ingresso un UBX-ACK-ACK/NAK relativo al comando
    (msg_class, msg_id) appena inviato. Ritorna True (ACK), False (NAK) o
    None se non arriva nulla entro il timeout (il modulo potrebbe non
    generare ACK per quel messaggio, o il baud rate/porta non è corretto)."""
    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline:
        chunk = ser.read(64)
        if chunk:
            buf += chunk
        while True:
            idx = buf.find(bytes([UBX_SYNC1, UBX_SYNC2]))
            if idx < 0 or len(buf) < idx + 6:
                break
            cls_, id_ = buf[idx + 2], buf[idx + 3]
            length = struct.unpack("<H", buf[idx + 4:idx + 6])[0]
            frame_len = 6 + length + 2
            if len(buf) < idx + frame_len:
                break
            payload = buf[idx + 6:idx + 6 + length]
            buf = buf[idx + frame_len:]
            if cls_ == ACK_CLASS and length == 2 and payload[0] == msg_class and payload[1] == msg_id:
                return id_ == ACK_ACK
        if not chunk:
            time.sleep(0.02)
    return None


def _send(ser, msg_class, msg_id, payload):
    frame = _frame(msg_class, msg_id, payload)
    ser.reset_input_buffer()
    ser.write(frame)
    ack = _read_ack(ser, msg_class, msg_id)
    label = {True: "ACK", False: "NAK (comando rifiutato dal modulo!)",
             None: "nessuna risposta entro il timeout"}[ack]
    print(f"[ublox] >> UBX {msg_class:02X} {msg_id:02X} ({len(payload)} byte payload) -> {label}", flush=True)
    time.sleep(0.05)
    return ack


def _cfg_msg(ser, msg_class, msg_id, rate):
    _send(ser, *CFG_MSG, bytes([msg_class, msg_id, rate]))


def configure_rtcm(port, baud):
    with serial.Serial(port, baud, timeout=1) as ser:
        for rtcm_type, ubx_id in RTCM_MSG_IDS.items():
            rate = 5 if rtcm_type == 1005 else 1  # 1005 ogni 5 epoche, MSM ogni epoca
            _cfg_msg(ser, 0xF5, ubx_id, rate)


def configure_nmea(port, baud):
    with serial.Serial(port, baud, timeout=1) as ser:
        for _name, ubx_id in NMEA_MSG_IDS.items():
            _cfg_msg(ser, 0xF0, ubx_id, 1)


def _split_main_hp(value, scale):
    """Divide un valore float in (parte principale, parte ad alta
    precisione) secondo il rapporto 1:100 usato da TMODE3 per lat/lon/alt
    (es. 1e-7 grado + hp in 1e-9 grado, oppure cm + hp in 0.1mm)."""
    total = round(value * scale)
    return divmod(total, 100)


def _tmode3_payload(mode, lat=0.0, lon=0.0, height=0.0, fixed_pos_acc=0):
    flags = mode
    if mode == 2:  # fixed, con coordinate in LLA (non ECEF)
        flags |= 1 << 8
    lat_main, lat_hp = _split_main_hp(lat, 1e9)      # 1e-7 deg + hp 1e-9 deg
    lon_main, lon_hp = _split_main_hp(lon, 1e9)
    height_main, height_hp = _split_main_hp(height, 1e4)  # cm + hp 0.1mm
    return struct.pack(
        "<BBHiiibbbBIII8s",
        0,               # version
        0,               # reserved1
        flags,
        lat_main, lon_main, height_main,
        lat_hp, lon_hp, height_hp,
        0,               # reserved2
        fixed_pos_acc,   # fixedPosAcc, unità 0.1mm
        0, 0,            # svinMinDur, svinAccLimit (non usati fuori dal survey-in nativo)
        b"\x00" * 8,     # reserved3
    )


def set_rover_mode(port, baud):
    """Disabilita TMODE3 (mode=0): il ricevitore torna a posizionamento
    standalone, necessario prima di un survey-in software (letture GGA
    mediate lato add-on, non il survey-in nativo del modulo)."""
    with serial.Serial(port, baud, timeout=1) as ser:
        _send(ser, *CFG_TMODE3, _tmode3_payload(mode=0))


def set_fixed_base(port, baud, lat, lon, height):
    """Imposta TMODE3 in modalità fissa (mode=2) con le coordinate LLA
    indicate (gradi decimali WGS84, altezza ellissoidica in metri)."""
    with serial.Serial(port, baud, timeout=1) as ser:
        _send(ser, *CFG_TMODE3, _tmode3_payload(mode=2, lat=lat, lon=lon, height=height))
