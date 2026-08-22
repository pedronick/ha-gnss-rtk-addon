"""
I moduli dell'add-on (main.py, nmea.py, drivers/, ecc.) sono import "piatti"
senza package: nel container girano tutti da /app con quel layout esatto.
Per testarli fuori da Home Assistant/Docker basta aggiungere la cartella
dell'add-on al sys.path prima di importarli — nessun'altra dipendenza da
Home Assistant, Supervisor o hardware reale è necessaria per questa suite.
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
    """Una pty (pseudo-terminale) che si comporta come una vera porta
    seriale per pyserial: master_fd per il lato 'strumento di test' (invia/
    riceve byte come farebbe il modulo GNSS reale), slave_path per il lato
    che il codice sotto test apre con serial.Serial(slave_path, ...)."""
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    yield master_fd, slave_path
    # Alcuni test chiudono già master_fd per simulare uno scollegamento:
    # in quel caso os.close() qui solleverebbe OSError, da ignorare.
    for fd in (master_fd, slave_fd):
        try:
            os.close(fd)
        except OSError:
            pass
