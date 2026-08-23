"""
Unicore driver, for the UM980/UM982 family (same ASCII command set).

WARNING: the exact syntax of Unicore commands may vary between
firmware/revisions. Always check in the add-on log (stdout) that every
command receives a confirmation response from the module; if a command
is not recognized, check the UM982 command manual and adapt the strings
below (some revisions use "rtcm1077 1" directly instead of
"log rtcm1077 ontime 1"). Driver contract: see drivers/base.py.
"""

import time

import serial

NAME = "Unicore UM980/UM982"


def send_commands(port, baud, commands):
    with serial.Serial(port, baud, timeout=1) as ser:
        time.sleep(0.3)
        ser.reset_input_buffer()
        for cmd in commands:
            print(f"[unicore] >> {cmd}", flush=True)
            ser.write((cmd + "\r\n").encode("ascii"))
            time.sleep(0.2)
            resp = ser.read(ser.in_waiting or 1)
            if resp:
                print("[unicore] <<", resp.decode("ascii", errors="replace").strip(), flush=True)


def configure_rtcm(port, baud):
    """Enables RTCM3 (station coordinates + MSM7 observations) on the given port."""
    send_commands(port, baud, [
        "log rtcm1005 ontime 10",
        "log rtcm1077 ontime 1",
        "log rtcm1087 ontime 1",
        "log rtcm1097 ontime 1",
        "log rtcm1127 ontime 1",
        "saveconfig",
    ])


def configure_nmea(port, baud):
    """Enables GGA (fix/satellites), GST (accuracy), GSV (satellites in
    view, for the skyplot) and GSA (satellites used in the fix + DOP) on
    the given port.

    If rtcm_port == nmea_port, this function must be called after
    configure_rtcm() on the same port: the two message sets add up.
    """
    send_commands(port, baud, [
        "log gga ontime 1",
        "log gst ontime 1",
        "log gsv ontime 1",
        "log gsa ontime 1",
        "saveconfig",
    ])


def set_rover_mode(port, baud):
    """Puts the receiver in rover mode (standalone positioning), needed
    before a survey-in to get fixes not constrained by an already fixed
    base position."""
    send_commands(port, baud, ["mode rover"])


def set_fixed_base(port, baud, lat, lon, height):
    """Sets the module to BASE mode with a fixed position (WGS84 decimal
    degrees, ellipsoidal height in meters) and saves the configuration."""
    send_commands(port, baud, [
        f"mode base {lat:.8f} {lon:.8f} {height:.3f}",
        "saveconfig",
    ])
