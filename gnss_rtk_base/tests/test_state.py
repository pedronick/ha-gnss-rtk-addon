import time

import nmea
from state import SharedState


def test_snapshot_reflects_gga_gst_gsa_gsv():
    s = SharedState()
    s.update_gga({"quality": 4, "num_sv": 12})
    s.update_gst(0.01)
    s.update_gsv([{"prn": 10, "constellation": "GPS", "elevation": 63, "azimuth": 137, "snr": 42}])
    s.update_gsa({"used_prns": {10}, "pdop": 1.5, "hdop": 0.9, "vdop": 1.2})

    snap = s.snapshot(nmea.fix_label)
    assert snap["fix_quality"] == 4
    assert snap["fix_label"] == "Fix"
    assert snap["num_sv"] == 12
    assert snap["accuracy_m"] == 0.01
    assert snap["pdop"] == 1.5
    assert len(snap["satellites"]) == 1
    assert snap["satellites"][0]["used"] is True


def test_satellite_not_used_when_prn_absent_from_gsa():
    s = SharedState()
    s.update_gsv([{"prn": 26, "constellation": "GPS", "elevation": 22, "azimuth": 308, "snr": 19}])
    s.update_gsa({"used_prns": {10, 7}, "pdop": None, "hdop": None, "vdop": None})
    snap = s.snapshot(nmea.fix_label)
    assert snap["satellites"][0]["used"] is False


def test_stale_satellites_are_pruned_from_snapshot():
    s = SharedState()
    s.update_gsv([{"prn": 1, "constellation": "GPS", "elevation": 10, "azimuth": 10, "snr": 30}])
    # Force the satellite to appear "stale" by rolling back its timestamp.
    key = ("GPS", 1)
    s._sats[key]["last_seen"] = time.time() - 100
    snap = s.snapshot(nmea.fix_label)
    assert snap["satellites"] == []
