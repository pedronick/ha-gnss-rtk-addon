import datetime as dt
import gzip
import re

import ppp


def test_gps_week_dow_epoch_and_known_reference():
    # By definition, the GPS epoch itself is week 0, day 0.
    assert ppp.gps_week_dow(ppp.GPS_EPOCH) == (0, 0)
    assert ppp.gps_week_dow(ppp.GPS_EPOCH + dt.timedelta(days=7)) == (1, 0)
    assert ppp.gps_week_dow(ppp.GPS_EPOCH + dt.timedelta(days=3)) == (0, 3)
    # November 27, 2022 is the start of GPS week 2238, when IGS
    # introduced the new long-form naming convention for products.
    assert ppp.gps_week_dow(dt.date(2022, 11, 27)) == (2238, 0)


def test_build_igs_names_final_and_rapid():
    sp3, clk = ppp.build_igs_names(dt.date(2024, 1, 15), "FIN")
    assert sp3 == "IGS0OPSFIN_20240150000_01D_05M_ORB.SP3.gz"
    assert clk == "IGS0OPSFIN_20240150000_01D_30S_CLK.CLK.gz"

    sp3_rap, _ = ppp.build_igs_names(dt.date(2024, 1, 15), "RAP")
    assert "IGS0OPSRAP" in sp3_rap
    assert "15M" in sp3_rap


def test_collect_raw_files_filters_by_time_window(tmp_path):
    # Hourly files for 2024-01-15 from 00 to 05 UTC.
    for h in range(6):
        (tmp_path / f"gnssbase_202401150{h}.rtcm3").write_bytes(b"x")
    (tmp_path / "other_file.rtcm3").write_bytes(b"x")  # must not be selected

    start = dt.datetime(2024, 1, 15, 1, tzinfo=dt.timezone.utc).timestamp()
    end = dt.datetime(2024, 1, 15, 3, tzinfo=dt.timezone.utc).timestamp()
    selected = ppp.collect_raw_files(str(tmp_path), start, end)

    # With the one-hour margin, hours 00-04 are expected (01-1h .. 03+1h).
    hours = sorted(int(re.search(r"gnssbase_\d{8}(\d{2})\.rtcm3$", p).group(1)) for p in selected)
    assert hours == [0, 1, 2, 3, 4]


def test_concat_raw_files(tmp_path):
    f1, f2 = tmp_path / "a.rtcm3", tmp_path / "b.rtcm3"
    f1.write_bytes(b"AAA")
    f2.write_bytes(b"BBB")
    out = tmp_path / "out.rtcm3"
    ppp.concat_raw_files([str(f1), str(f2)], out)
    assert out.read_bytes() == b"AAABBB"


def test_parse_obs_dates_from_minimal_rinex_header(tmp_path):
    obs = tmp_path / "campaign.obs"
    obs.write_text(
        "     3.04           OBSERVATION DATA    M: MIXED            RINEX VERSION\n"
        "  2024    01    15    00    00    0.0000000     GPS         TIME OF FIRST OBS\n"
        "  2024    01    15    23    59   30.0000000     GPS         TIME OF LAST OBS\n"
        "> 2024 01 15 00 00 0.0000000  0 12\n"
    )
    dates = ppp.parse_obs_dates(str(obs))
    assert dates == [dt.date(2024, 1, 15)]


def test_parse_obs_dates_raises_without_header(tmp_path):
    obs = tmp_path / "campaign.obs"
    obs.write_text("no useful header here\n> 2024 01 15 00 00 0.0 0 12\n")
    try:
        ppp.parse_obs_dates(str(obs))
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_gunzip_roundtrip(tmp_path):
    src = tmp_path / "data.txt.gz"
    with gzip.open(src, "wb") as f:
        f.write(b"test content")
    out = ppp.gunzip(src)
    assert out.name == "data.txt"
    assert out.read_bytes() == b"test content"


def test_parse_last_position_returns_last_valid_epoch(tmp_path):
    pos = tmp_path / "result.pos"
    pos.write_text(
        "% RTKLIB header, to ignore\n"
        "2024/01/15 00:00:00.000   45.1000000    9.7000000   100.000   5   4\n"
        "2024/01/15 00:00:01.000   45.1234500    9.7654300   123.456   1   8\n"
        "\n"
    )
    lat, lon, height = ppp.parse_last_position(str(pos))
    assert lat == 45.12345
    assert lon == 9.76543
    assert height == 123.456


def test_try_download_uses_first_working_mirror(monkeypatch, tmp_path):
    calls = []

    class FakeResponse:
        def __init__(self, status_code, content):
            self.status_code = status_code
            self.content = content

    def fake_get(url, timeout):
        calls.append(url)
        if "broken-mirror" in url:
            return FakeResponse(404, b"")
        return FakeResponse(200, b"x" * 2000)

    monkeypatch.setattr(ppp.requests, "get", fake_get)

    dest = tmp_path / "product.gz"
    ok = ppp.try_download(["https://broken-mirror/x", "https://good-mirror/x"], dest)

    assert ok is True
    assert dest.read_bytes() == b"x" * 2000
    assert calls == ["https://broken-mirror/x", "https://good-mirror/x"]


def test_try_download_returns_false_if_all_mirrors_fail(monkeypatch, tmp_path):
    class FakeResponse:
        status_code = 404
        content = b""

    monkeypatch.setattr(ppp.requests, "get", lambda url, timeout: FakeResponse())

    dest = tmp_path / "product.gz"
    ok = ppp.try_download(["https://a/x", "https://b/x"], dest)
    assert ok is False
    assert not dest.exists()
