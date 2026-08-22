"""Registro dei driver ricevitore disponibili. Ogni driver è un modulo che
espone configure_rtcm/configure_nmea/set_rover_mode/set_fixed_base (vedi
base.py per il contratto completo)."""

from . import ublox, unicore

DRIVERS = {
    "unicore_um98x": unicore,
    "ublox_zedf9p": ublox,
}


def get_driver(name):
    try:
        return DRIVERS[name]
    except KeyError:
        raise ValueError(
            f"Ricevitore non supportato: {name!r}. Opzioni disponibili: {sorted(DRIVERS)}"
        ) from None
