"""
The add-on modules (main.py, nmea.py, drivers/, etc.) are "flat" imports
with no package: in the container they all run from /app with that exact
layout. To test them outside Home Assistant/Docker it's enough to add the
add-on folder to sys.path before importing them — no other dependency on
Home Assistant, Supervisor, or real hardware is needed for this suite.
"""

import os
import pty
import sys
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parent.parent
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))


@pytest.fixture
def fake_serial_pair():
    """A pty (pseudo-terminal) that behaves like a real serial port for
    pyserial: master_fd is the 'test harness' side (sends/receives bytes
    like the real GNSS module would), slave_path is the side the code
    under test opens with serial.Serial(slave_path, ...)."""
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    yield master_fd, slave_path
    # Some tests already close master_fd to simulate a disconnection:
    # in that case os.close() here would raise OSError, which we ignore.
    for fd in (master_fd, slave_fd):
        try:
            os.close(fd)
        except OSError:
            pass
