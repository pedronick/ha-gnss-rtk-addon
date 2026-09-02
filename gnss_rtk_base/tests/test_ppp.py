import datetime as dt
import gzip
import re

import pytest

import ppp


def test_ppp_conf_template_uses_valid_rtklib_option_values():
    """Regression: pos1-frequency=l1+l2 and pos1-ionoopt=iflc aren't
    valid RTKLIB option values - a real rnx2rtkp run rejected them with
    "invalid option value" and silently fell back to a default that
    produced an all-Q=0 result.pos (no fix at all), found from a real PPP
    campaign on a user's Home Assistant instance. Verified against
    RTKLIB's actual enum strings in src/options.c (FRQOPT/IONOPT): the
    correct values are l1+2 (dual-frequency) and dual-freq
    (ionosphere-free combination via dual-frequency observations) -
    confirmed with a real rnx2rtkp run producing no more "invalid option
    value" warnings."""
    assert "pos1-frequency     =l1+2" in ppp.PPP_CONF_TEMPLATE
    assert "pos1-ionoopt       =dual-freq" in ppp.PPP_CONF_TEMPLATE
    assert "l1+l2" not in ppp.PPP_CONF_TEMPLATE
    assert "=iflc" not in ppp.PPP_CONF_TEMPLATE


def test_gps_week_dow_epoch_and_known_reference():
    # By definition, the GPS epoch itself is week 0, day 0.
    assert ppp.gps_week_dow(ppp.GPS_EPOCH) == (0, 0)
    assert ppp.gps_week_dow(ppp.GPS_EPOCH + dt.timedelta(days=7)) == (1, 0)
    assert ppp.gps_week_dow(ppp.GPS_EPOCH + dt.timedelta(days=3)) == (0, 3)
    # November 27, 2022 is the start of GPS week 2238, when IGS
    # introduced the new long-form naming convention for products.
    assert ppp.gps_week_dow(dt.date(2022, 11, 27)) == (2238, 0)


def test_build_igs_names_final_and_rapid():
    # Verified against a real directory listing (BKG mirror): FIN only
    # publishes 15M orbits (not 05M) and RAP only publishes 05M clocks
    # (not 30S) - only FIN has a 30S clock variant.
    sp3, clk = ppp.build_igs_names(dt.date(2024, 1, 15), "FIN")
    assert sp3 == "IGS0OPSFIN_20240150000_01D_15M_ORB.SP3.gz"
    assert clk == "IGS0OPSFIN_20240150000_01D_30S_CLK.CLK.gz"

    sp3_rap, clk_rap = ppp.build_igs_names(dt.date(2024, 1, 15), "RAP")
    assert sp3_rap == "IGS0OPSRAP_20240150000_01D_15M_ORB.SP3.gz"
    assert clk_rap == "IGS0OPSRAP_20240150000_01D_05M_CLK.CLK.gz"


def test_fetch_precise_products_falls_back_from_fin_to_rap(monkeypatch, tmp_path):
    """The automatic PPP campaign processes the raw log right after
    logging ends, when IGS "final" products for that date are basically
    never published yet (~11-18 days latency) - it must fall back to
    "rapid" (~17-41h latency) instead of failing outright."""
    calls = []

    def fake_try_download(urls, dest):
        calls.append(dest.name)
        if "IGS0OPSFIN" in dest.name:
            return False  # simulates "not published yet"
        with gzip.open(dest, "wb") as f:
            f.write(b"fake product content")
        return True

    monkeypatch.setattr(ppp, "try_download", fake_try_download)
    (tmp_path / "igs20.atx").write_bytes(b"fake atx")  # skip the real ANTEX download

    sp3_paths, clk_paths, atx_path, tiers_used = ppp.fetch_precise_products(
        [dt.date(2024, 1, 15)], tmp_path)

    assert len(sp3_paths) == 1 and len(clk_paths) == 1
    assert "IGS0OPSRAP" in sp3_paths[0].name
    assert sp3_paths[0].read_bytes() == b"fake product content"
    assert any("IGS0OPSFIN" in c for c in calls), "must try FIN first"
    assert any("IGS0OPSRAP" in c for c in calls), "must fall back to RAP"
    assert tiers_used == {dt.date(2024, 1, 15): "RAP"}


def test_fetch_precise_products_raises_when_no_tier_available(monkeypatch, tmp_path):
    monkeypatch.setattr(ppp, "try_download", lambda urls, dest: False)

    with pytest.raises(RuntimeError, match="No IGS products available"):
        ppp.fetch_precise_products([dt.date(2024, 1, 15)], tmp_path)


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
        def __init__(self, status_code, content, headers=None):
            self.status_code = status_code
            self.content = content
            self.headers = headers or {}

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


def test_try_download_rejects_html_error_page_with_200_status(monkeypatch, tmp_path):
    """Regression: a mirror requiring authentication (e.g. CDDIS without
    NASA Earthdata credentials) can serve an HTML login/error page with a
    200 status, long enough to pass a bare length check - this made a
    real PPP campaign fail deep inside gzip decompression with a
    confusing "Not a gzipped file (b'<!')" instead of a clear "download
    failed"."""
    class FakeResponse:
        def __init__(self, status_code, content, headers=None):
            self.status_code = status_code
            self.content = content
            self.headers = headers or {}

    html_page = b"<!DOCTYPE html><html><body>Please log in</body></html>" + b" " * 1000

    def fake_get(url, timeout):
        if "html-content-type" in url:
            return FakeResponse(200, b"x" * 2000, headers={"Content-Type": "text/html; charset=utf-8"})
        return FakeResponse(200, html_page)

    monkeypatch.setattr(ppp.requests, "get", fake_get)

    dest = tmp_path / "product.gz"
    assert ppp.try_download(["https://auth-required-mirror/x"], dest) is False
    assert not dest.exists()

    dest2 = tmp_path / "product2.gz"
    assert ppp.try_download(["https://html-content-type/x"], dest2) is False
    assert not dest2.exists()


def test_try_download_returns_false_if_all_mirrors_fail(monkeypatch, tmp_path):
    class FakeResponse:
        status_code = 404
        content = b""
        headers = {}

    monkeypatch.setattr(ppp.requests, "get", lambda url, timeout: FakeResponse())

    dest = tmp_path / "product.gz"
    ok = ppp.try_download(["https://a/x", "https://b/x"], dest)
    assert ok is False
    assert not dest.exists()
