"""
u-blox driver (binary UBX protocol), for modules like ZED-F9P/F9R/M8P.

Uses UBX-CFG-MSG messages (class 0x06 id 0x01) to enable RTCM3/NMEA on
the current port, and UBX-CFG-TMODE3 (0x06 0x71) for rover/fixed-base
mode with LLA coordinates — these are the "legacy" messages of the UBX
protocol, also supported by newer modules (F9P) in addition to
CFG-VALSET, so they should work across the whole M8P/F9P/F9R family.

WARNING: the RTCM3/NMEA message IDs and the TMODE3 payload layout are
taken from public u-blox documentation (Interface Description) at the
time of writing, but must be verified against the specific manual of
your module/firmware before production use. Every command now waits for
the UBX-ACK-ACK/NAK response (class 0x05) and logs it explicitly: a
"NAK" or "no response" log is a reliable signal that something in the
command was not accepted by the module, to be checked against your
Interface Manual or with u-center. Driver contract: see drivers/base.py.
"""

import struct
import time

import serial

NAME = "u-blox ZED-F9P / M8P (UBX protocol)"

UBX_SYNC1, UBX_SYNC2 = 0xB5, 0x62
CFG_MSG = (0x06, 0x01)
CFG_TMODE3 = (0x06, 0x71)
ACK_CLASS = 0x05
ACK_ACK, ACK_NAK = 0x01, 0x00

# RTCM3 message IDs in UBX class 0xF5.
RTCM_MSG_IDS = {
    1005: 0x05,
    1077: 0x4D,
    1087: 0x4F,
    1097: 0x61,
    1127: 0x7F,
}
# Standard NMEA message IDs in UBX class 0xF0.
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
    """Looks in the incoming stream for a UBX-ACK-ACK/NAK relating to the
    (msg_class, msg_id) command just sent. Returns True (ACK), False
    (NAK), or None if nothing arrives within the timeout (the module may
    not generate an ACK for that message, or the baud rate/port is
    wrong)."""
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
    label = {True: "ACK", False: "NAK (command rejected by the module!)",
             None: "no response within timeout"}[ack]
    print(f"[ublox] >> UBX {msg_class:02X} {msg_id:02X} ({len(payload)} byte payload) -> {label}", flush=True)
    time.sleep(0.05)
    return ack


def _cfg_msg(ser, msg_class, msg_id, rate):
    _send(ser, *CFG_MSG, bytes([msg_class, msg_id, rate]))


def reset(port, baud):
    """No-op for this driver, unlike drivers/unicore.py's reset(). Unicore
    needs it because its "log" commands are purely additive (an old
    manually-enabled message keeps streaming forever unless explicitly
    cleared with "unlogall"). This driver instead sets an explicit rate
    for each message ID it manages via UBX-CFG-MSG in configure_rtcm()/
    configure_nmea() - calling those already authoritatively overwrites
    the rate for exactly those IDs, with nothing left to separately
    clear. A real factory-reset command exists (UBX-CFG-CFG, reverting
    RAM+BBR+Flash config layers) but isn't used here: it would also wipe
    unrelated settings (e.g. dynamic model, RF calibration) and hasn't
    been verified against real ZED-F9P/M8P hardware (see the module
    warning above) - safer to leave it as a documented no-op than guess."""


def configure_rtcm(port, baud):
    with serial.Serial(port, baud, timeout=1) as ser:
        for rtcm_type, ubx_id in RTCM_MSG_IDS.items():
            rate = 5 if rtcm_type == 1005 else 1  # 1005 every 5 epochs, MSM every epoch
            _cfg_msg(ser, 0xF5, ubx_id, rate)


def configure_nmea(port, baud):
    with serial.Serial(port, baud, timeout=1) as ser:
        for _name, ubx_id in NMEA_MSG_IDS.items():
            _cfg_msg(ser, 0xF0, ubx_id, 1)


def _split_main_hp(value, scale):
    """Splits a float value into (main part, high-precision part)
    following the 1:100 ratio used by TMODE3 for lat/lon/alt (e.g. 1e-7
    degree + hp in 1e-9 degree, or cm + hp in 0.1mm)."""
    total = round(value * scale)
    return divmod(total, 100)


def _tmode3_payload(mode, lat=0.0, lon=0.0, height=0.0, fixed_pos_acc=0):
    flags = mode
    if mode == 2:  # fixed, with coordinates in LLA (not ECEF)
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
        fixed_pos_acc,   # fixedPosAcc, unit 0.1mm
        0, 0,            # svinMinDur, svinAccLimit (unused outside native survey-in)
        b"\x00" * 8,     # reserved3
    )


def set_rover_mode(port, baud):
    """Disables TMODE3 (mode=0): the receiver goes back to standalone
    positioning, needed before a software survey-in (GGA readings
    averaged on the add-on side, not the module's native survey-in)."""
    with serial.Serial(port, baud, timeout=1) as ser:
        _send(ser, *CFG_TMODE3, _tmode3_payload(mode=0))


def set_fixed_base(port, baud, lat, lon, height):
    """Sets TMODE3 to fixed mode (mode=2) with the given LLA coordinates
    (WGS84 decimal degrees, ellipsoidal height in meters)."""
    with serial.Serial(port, baud, timeout=1) as ser:
        _send(ser, *CFG_TMODE3, _tmode3_payload(mode=2, lat=lat, lon=lon, height=height))
