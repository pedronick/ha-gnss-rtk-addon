import os
import struct
import threading
import time

import pytest

import drivers.ublox as ublox


def test_checksum_matches_reference_implementation():
    frame = ublox._frame(0x06, 0x01, bytes([0xF0, 0x00, 1]))
    assert frame[0:2] == bytes([0xB5, 0x62])
    assert frame[2:4] == bytes([0x06, 0x01])
    length = struct.unpack("<H", frame[4:6])[0]
    assert length == 3
    assert frame[6:6 + length] == bytes([0xF0, 0x00, 1])

    ck_a = ck_b = 0
    for byte in frame[2:-2]:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    assert (frame[-2], frame[-1]) == (ck_a, ck_b)


@pytest.mark.parametrize("lat", [45.1234567, -12.0000001, 0.0, 89.9999999])
def test_split_main_hp_lat_lon_roundtrip(lat):
    main, hp = ublox._split_main_hp(lat, 1e9)
    reconstructed = (main * 100 + hp) / 1e9
    assert abs(reconstructed - lat) < 1e-9


@pytest.mark.parametrize("height", [123.456, -10.001, 0.0, 4500.0])
def test_split_main_hp_height_roundtrip(height):
    main, hp = ublox._split_main_hp(height, 1e4)
    reconstructed = (main * 100 + hp) / 1e4
    assert abs(reconstructed - height) < 1e-4


def test_tmode3_payload_is_40_bytes():
    payload = ublox._tmode3_payload(mode=2, lat=45.1234567, lon=9.7654321, height=123.456)
    assert len(payload) == 40


def _fake_ublox_module(master_fd, nak_first_rtcm_cfg=True):
    """Simulates a u-blox module: replies ACK to every command, except to
    the first UBX-CFG-MSG with RTCM class (0xF5), to which it replies
    NAK, to verify that the driver correctly distinguishes the two
    cases."""
    state = {"first_f5": nak_first_rtcm_cfg}

    def _run():
        buf = b""
        while True:
            try:
                chunk = os.read(master_fd, 256)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while True:
                idx = buf.find(bytes([0xB5, 0x62]))
                if idx < 0 or len(buf) < idx + 6:
                    break
                cls_, id_ = buf[idx + 2], buf[idx + 3]
                length = struct.unpack("<H", buf[idx + 4:idx + 6])[0]
                frame_len = 6 + length + 2
                if len(buf) < idx + frame_len:
                    break
                payload = buf[idx + 6:idx + 6 + length]
                buf = buf[idx + frame_len:]

                ack_id = 0x01  # ACK di default
                if cls_ == 0x06 and id_ == 0x01 and length == 3 and payload[0] == 0xF5 and state["first_f5"]:
                    ack_id = 0x00  # NAK sul primo CFG-MSG relativo a RTCM
                    state["first_f5"] = False

                ack_frame = ublox._frame(0x05, ack_id, bytes([cls_, id_]))
                try:
                    os.write(master_fd, ack_frame)
                except OSError:
                    return

    threading.Thread(target=_run, daemon=True).start()


def test_configure_rtcm_detects_ack_and_nak(fake_serial_pair, capsys):
    master_fd, slave_path = fake_serial_pair
    _fake_ublox_module(master_fd)

    ublox.configure_rtcm(slave_path, 115200)
    time.sleep(0.3)

    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "UBX 06 01" in l]
    assert len(lines) == 5  # 1005,1077,1087,1097,1127
    nak_lines = [l for l in lines if "NAK" in l]
    ack_lines = [l for l in lines if l.endswith("-> ACK")]
    assert len(nak_lines) == 1
    assert len(ack_lines) == 4


def test_read_ack_returns_none_without_reply(fake_serial_pair):
    """If the module doesn't respond at all (e.g. wrong baud rate), the
    driver must not block indefinitely: it must return None within the
    expected timeout."""
    import serial

    master_fd, slave_path = fake_serial_pair
    with serial.Serial(slave_path, 115200, timeout=1) as ser:
        ack = ublox._read_ack(ser, 0x06, 0x01, timeout=0.3)
    assert ack is None
