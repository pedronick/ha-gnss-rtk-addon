"""Shared helper for driver tests: intercepts on the 'master' side of a
pty everything the driver writes to the serial port, immediately
replying with a fake ack so that the driver's reads (which have a
timeout) don't have to wait for it needlessly."""

import os
import threading


def start_ascii_capture(master_fd, ack=b"<OK\r\n"):
    """For drivers with ASCII commands terminated by \\r\\n (e.g. Unicore).
    Returns the list (mutable, populated in real time) of received
    commands as strings, without the terminator."""
    commands = []

    def _run():
        buf = b""
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                commands.append(line.decode())
            try:
                os.write(master_fd, ack)
            except OSError:
                return

    threading.Thread(target=_run, daemon=True).start()
    return commands
