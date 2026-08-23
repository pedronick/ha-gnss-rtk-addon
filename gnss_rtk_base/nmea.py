"""Minimal parsing of NMEA GGA/GST/GSV/GSA sentences — standard NMEA-0183,
valid for any GNSS receiver (not specific to a driver)."""

FIX_QUALITY_LABELS = {
    0: "No Fix",
    1: "Single",
    2: "DGPS",
    3: "PPS",
    4: "Fix",       # RTK fixed
    5: "Float",     # RTK float
    6: "Estimated",
    7: "Manual",
    8: "Simulation",
}


def fix_label(quality):
    return FIX_QUALITY_LABELS.get(quality, f"Unknown ({quality})")


def _to_deg(value, hemisphere):
    if not value:
        return None
    dot = value.find(".")
    if dot < 2:
        return None
    deg_len = dot - 2
    deg = float(value[:deg_len])
    minutes = float(value[deg_len:])
    dec = deg + minutes / 60.0
    if hemisphere in ("S", "W"):
        dec = -dec
    return dec


def parse_gga(line):
    line = line.strip()
    if not line.startswith("$") or "GGA" not in line[1:6]:
        return None
    body = line.split("*")[0]
    parts = body.split(",")
    if len(parts) < 10:
        return None
    try:
        quality = int(parts[6]) if parts[6] else 0
        num_sv = int(parts[7]) if parts[7] else 0
        lat = _to_deg(parts[2], parts[3])
        lon = _to_deg(parts[4], parts[5])
        alt = float(parts[9]) if parts[9] else None
    except (ValueError, IndexError):
        return None
    return {"quality": quality, "num_sv": num_sv, "lat": lat, "lon": lon, "alt": alt}


CONSTELLATION_BY_TALKER = {
    "GP": "GPS",
    "GL": "GLONASS",
    "GA": "Galileo",
    "GB": "BeiDou",
    "BD": "BeiDou",
    "GQ": "QZSS",
    "GI": "NavIC",
    "GN": "Multi",
}


def parse_gsv(line):
    """Returns a list of satellites from a GSV sentence (one of the
    possible groups of 4 sent per sentence): [{prn, constellation,
    elevation, azimuth, snr}, ...]. Must be accumulated over time by the
    caller, because a full constellation is spread across several GSV
    sentences."""
    line = line.strip()
    if not line.startswith("$") or "GSV" not in line[1:6]:
        return []
    talker = line[1:3]
    body = line.split("*")[0]
    parts = body.split(",")
    if len(parts) < 4:
        return []
    constellation = CONSTELLATION_BY_TALKER.get(talker, talker)
    sats = []
    # From field 4 onward, groups of 4: prn, elevation, azimuth, snr
    for i in range(4, len(parts) - 3, 4):
        prn, elev, az, snr = parts[i:i + 4]
        if not prn:
            continue
        try:
            sats.append({
                "prn": int(prn),
                "constellation": constellation,
                "elevation": int(elev) if elev else None,
                "azimuth": int(az) if az else None,
                "snr": int(snr) if snr else None,
            })
        except ValueError:
            continue
    return sats


def parse_gsa(line):
    """Returns the set of PRNs used in the current solution (NMEA
    numbering, not distinguished by constellation) plus the DOP values,
    from a GSA sentence."""
    line = line.strip()
    if not line.startswith("$") or "GSA" not in line[1:6]:
        return None
    body = line.split("*")[0]
    parts = body.split(",")
    if len(parts) < 17:
        return None
    used = set()
    for field in parts[3:15]:
        if field:
            try:
                used.add(int(field))
            except ValueError:
                pass
    try:
        pdop = float(parts[15]) if parts[15] else None
        hdop = float(parts[16]) if parts[16] else None
        vdop = float(parts[17]) if len(parts) > 17 and parts[17] else None
    except ValueError:
        pdop = hdop = vdop = None
    return {"used_prns": used, "pdop": pdop, "hdop": hdop, "vdop": vdop}


def parse_gst(line):
    line = line.strip()
    if not line.startswith("$") or "GST" not in line[1:6]:
        return None
    body = line.split("*")[0]
    parts = body.split(",")
    if len(parts) < 8:
        return None
    try:
        std_lat = float(parts[6]) if parts[6] else None
        std_lon = float(parts[7]) if parts[7] else None
    except ValueError:
        return None
    if std_lat is None or std_lon is None:
        return None
    accuracy = (std_lat ** 2 + std_lon ** 2) ** 0.5
    return {"accuracy_m": round(accuracy, 3)}
