"""In-memory shared state between the NMEA monitor and the skyplot web
server. Satellites seen via GSV are kept for a short period (until newer
updates arrive) because a full constellation is spread across several
consecutive GSV sentences."""

import threading
import time

SAT_STALE_AFTER_S = 8


class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self.fix_quality = 0
        self.num_sv = 0
        self.accuracy_m = None
        self.pdop = self.hdop = self.vdop = None
        self._sats = {}   # (constellation, prn) -> dict with last_seen
        self._used_prns = set()

    def update_gga(self, fix):
        with self._lock:
            self.fix_quality = fix["quality"]
            self.num_sv = fix["num_sv"]

    def update_gst(self, accuracy_m):
        with self._lock:
            self.accuracy_m = accuracy_m

    def update_gsv(self, sats):
        now = time.time()
        with self._lock:
            for sat in sats:
                key = (sat["constellation"], sat["prn"])
                self._sats[key] = {**sat, "last_seen": now}

    def update_gsa(self, gsa):
        with self._lock:
            self._used_prns = gsa["used_prns"]
            self.pdop, self.hdop, self.vdop = gsa["pdop"], gsa["hdop"], gsa["vdop"]

    def snapshot(self, fix_label_fn):
        now = time.time()
        with self._lock:
            sats = []
            for (constellation, prn), sat in self._sats.items():
                if now - sat["last_seen"] > SAT_STALE_AFTER_S:
                    continue
                sats.append({
                    "prn": prn,
                    "constellation": constellation,
                    "elevation": sat["elevation"],
                    "azimuth": sat["azimuth"],
                    "snr": sat["snr"],
                    "used": prn in self._used_prns,
                })
            return {
                "fix_quality": self.fix_quality,
                "fix_label": fix_label_fn(self.fix_quality),
                "num_sv": self.num_sv,
                "accuracy_m": self.accuracy_m,
                "pdop": self.pdop,
                "hdop": self.hdop,
                "vdop": self.vdop,
                "satellites": sats,
            }
