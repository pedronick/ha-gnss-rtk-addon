import nmea

GGA_FIX = "$GPGGA,123519,4807.038,N,01131.000,E,4,12,0.9,545.4,M,46.9,M,,*7A"
GGA_NOFIX = "$GPGGA,123519,,,,,0,00,,,,,,,,*66"
GSV_1 = "$GPGSV,2,1,08,10,63,137,42,07,61,098,39,05,59,290,44,08,54,157,30*70"
GSV_2 = "$GPGSV,2,2,08,02,39,223,33,13,28,070,25,26,22,308,19,04,10,050,15*7A"
GSA = "$GPGSA,A,3,10,07,05,08,02,13,,,,,,,1.5,0.9,1.2*33"
GST = "$GPGST,123519,0.006,0.008,0.006,90.0,0.008,0.006,0.010*5D"


def test_parse_gga_fix():
    fix = nmea.parse_gga(GGA_FIX)
    assert fix["quality"] == 4
    assert fix["num_sv"] == 12
    assert abs(fix["lat"] - 48.1173) < 1e-3
    assert abs(fix["lon"] - 11.51667) < 1e-3
    assert fix["alt"] == 545.4


def test_parse_gga_no_fix():
    fix = nmea.parse_gga(GGA_NOFIX)
    assert fix["quality"] == 0
    assert fix["num_sv"] == 0
    assert fix["lat"] is None


def test_parse_gga_rejects_other_sentences():
    assert nmea.parse_gga("$GPGSA,A,3*00") is None
    assert nmea.parse_gga("not an NMEA sentence") is None


def test_fix_label_known_and_unknown_quality():
    assert nmea.fix_label(4) == "Fix"
    assert nmea.fix_label(5) == "Float"
    assert nmea.fix_label(1) == "Single"
    assert nmea.fix_label(99) == "Unknown (99)"


def test_parse_gsv_accumulated_across_two_sentences():
    sats_1 = nmea.parse_gsv(GSV_1)
    sats_2 = nmea.parse_gsv(GSV_2)
    all_sats = sats_1 + sats_2
    assert len(all_sats) == 8
    assert all(s["constellation"] == "GPS" for s in all_sats)
    prns = {s["prn"] for s in all_sats}
    assert prns == {10, 7, 5, 8, 2, 13, 26, 4}
    first = sats_1[0]
    assert first["prn"] == 10
    assert first["elevation"] == 63
    assert first["azimuth"] == 137
    assert first["snr"] == 42


def test_parse_gsa_used_prns_and_dop():
    gsa = nmea.parse_gsa(GSA)
    assert gsa["used_prns"] == {10, 7, 5, 8, 2, 13}
    assert gsa["pdop"] == 1.5
    assert gsa["hdop"] == 0.9
    assert gsa["vdop"] == 1.2


def test_parse_gst_accuracy():
    gst = nmea.parse_gst(GST)
    # sqrt(0.008^2 + 0.006^2) = 0.01
    assert gst["accuracy_m"] == 0.01
