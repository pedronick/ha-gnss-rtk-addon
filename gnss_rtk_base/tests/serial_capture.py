"""Helper condiviso per i test dei driver: intercetta sul lato 'master' di
una pty tutto ciò che il driver scrive sulla porta seriale, rispondendo
subito con un ack fittizio in modo che le letture del driver (che hanno un
timeout) non debbano aspettarlo inutilmente."""

import os
import threading


def start_ascii_capture(master_fd, ack=b"<OK\r\n"):
    """Per driver a comandi ASCII terminati da \\r\\n (es. Unicore).
    Ritorna la lista (mutabile, popolata in tempo reale) dei comandi
    ricevuti come stringhe, senza terminatore."""
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
