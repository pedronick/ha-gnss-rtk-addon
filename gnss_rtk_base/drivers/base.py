"""
Contract that every receiver driver must implement.

This is deliberately not an abstract base class: the drivers in this
project are modules with functions of the same name, not instances —
simpler to read/extend for a project of this size. This file only
documents the required signature; every driver in this package must
expose exactly these five functions:

    reset(port: str, baud: int) -> None
        Clears any message/log output previously configured on the
        receiver (from a prior manual test, a previous version of this
        add-on, etc.) before configure_rtcm()/configure_nmea() enable
        exactly what's needed. Without this, leftover configuration can
        accumulate silently across add-on restarts/reconfigurations -
        found on real UM982 hardware still emitting stray messages from
        an unrelated earlier session. If the receiver has nothing
        meaningful to clear (e.g. because configure_rtcm/configure_nmea
        already fully overwrite the specific messages it manages, one by
        one, rather than accumulating a log list), a no-op is a valid
        implementation - see drivers/ublox.py for why.

    configure_rtcm(port: str, baud: int) -> None
        Enables on the given port the minimum RTCM3 output required by
        this add-on: 1005 (station coordinates) + MSM (or equivalent)
        for GPS/GLONASS/Galileo/BeiDou.

    configure_nmea(port: str, baud: int) -> None
        Enables on the given port GGA, GST, GSV, GSA at ~1Hz (used by
        nmea.py for fix/satellites/accuracy/skyplot). If the receiver
        doesn't support one of these messages (e.g. no GST), it's fine
        to omit it: main.py already handles the absence of that data.

    set_rover_mode(port: str, baud: int) -> None
        Puts the receiver in standalone mode (no fixed base position),
        used before survey-in to get fixes not constrained by a
        previous position.

    set_fixed_base(port: str, baud: int, lat: float, lon: float, height: float) -> None
        Sets the receiver to base mode with a fixed position (WGS84
        decimal degrees, ellipsoidal height in meters) and saves the
        configuration if the receiver explicitly requires it.

A new driver for another receiver should be added as a new module in
this package (e.g. drivers/septentrio.py) with these same four
functions, then registered in drivers/__init__.py.
"""
