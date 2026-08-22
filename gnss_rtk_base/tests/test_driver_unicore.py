import time

import drivers.unicore as unicore
from serial_capture import start_ascii_capture


def test_configure_rtcm_sends_expected_commands(fake_serial_pair):
    master_fd, slave_path = fake_serial_pair
    commands = start_ascii_capture(master_fd)

    unicore.configure_rtcm(slave_path, 115200)
    time.sleep(0.3)

    assert commands == [
        "log rtcm1005 ontime 10",
        "log rtcm1077 ontime 1",
        "log rtcm1087 ontime 1",
        "log rtcm1097 ontime 1",
        "log rtcm1127 ontime 1",
        "saveconfig",
    ]


def test_configure_nmea_sends_expected_commands(fake_serial_pair):
    master_fd, slave_path = fake_serial_pair
    commands = start_ascii_capture(master_fd)

    unicore.configure_nmea(slave_path, 115200)
    time.sleep(0.3)

    assert commands == [
        "log gga ontime 1",
        "log gst ontime 1",
        "log gsv ontime 1",
        "log gsa ontime 1",
        "saveconfig",
    ]


def test_set_rover_mode_command(fake_serial_pair):
    master_fd, slave_path = fake_serial_pair
    commands = start_ascii_capture(master_fd)

    unicore.set_rover_mode(slave_path, 115200)
    time.sleep(0.3)

    assert commands == ["mode rover"]


def test_set_fixed_base_formats_coordinates(fake_serial_pair):
    master_fd, slave_path = fake_serial_pair
    commands = start_ascii_capture(master_fd)

    unicore.set_fixed_base(slave_path, 115200, 45.1234567, 9.7654321, 123.456)
    time.sleep(0.3)

    assert commands == [
        "mode base 45.12345670 9.76543210 123.456",
        "saveconfig",
    ]
