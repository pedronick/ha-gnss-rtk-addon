"""Registry of available receiver drivers. Each driver is a module that
exposes configure_rtcm/configure_nmea/set_rover_mode/set_fixed_base (see
base.py for the full contract)."""

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
            f"Unsupported receiver: {name!r}. Available options: {sorted(DRIVERS)}"
        ) from None
