import datetime as dt
import gzip
import os
import re
import subprocess

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


def test_collect_raw_files_uses_mtime_for_a_long_lived_file(tmp_path):
    """Regression: str2str only rotates the raw log hourly if its output
    path has an explicit "::S=1" swap option (see main.py's
    build_str2str_cmd()) - without it, str2str keeps appending to the
    same file indefinitely, so its *name* reflects only the hour it was
    first opened while its *content* (and mtime) can reach much further.
    Found from a real installation: a file named for hour 11 was still
    being written to over 2 hours later, and every window (of any size)
    missed it entirely once "now" drifted far enough past that one
    hour - matching by filename alone isn't enough."""
    old_named_file = tmp_path / "gnssbase_2024011500.rtcm3"
    old_named_file.write_bytes(b"x")
    still_being_written_at = dt.datetime(2024, 1, 15, 3, tzinfo=dt.timezone.utc).timestamp()
    os.utime(old_named_file, (still_being_written_at, still_being_written_at))

    start = dt.datetime(2024, 1, 15, 2, 30, tzinfo=dt.timezone.utc).timestamp()
    end = dt.datetime(2024, 1, 15, 3, tzinfo=dt.timezone.utc).timestamp()
    selected = ppp.collect_raw_files(str(tmp_path), start, end)

    assert selected == [str(old_named_file)]


def test_collect_raw_files_excludes_a_genuinely_old_untouched_file(tmp_path):
    old_file = tmp_path / "gnssbase_2024011000.rtcm3"
    old_file.write_bytes(b"x")
    old_ts = dt.datetime(2024, 1, 10, 0, tzinfo=dt.timezone.utc).timestamp()
    os.utime(old_file, (old_ts, old_ts))

    start = dt.datetime(2024, 1, 15, 0, tzinfo=dt.timezone.utc).timestamp()
    end = dt.datetime(2024, 1, 15, 6, tzinfo=dt.timezone.utc).timestamp()
    selected = ppp.collect_raw_files(str(tmp_path), start, end)

    assert selected == []


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


def test_run_quiet_suppresses_output_on_success_but_raises_with_it_on_failure(monkeypatch, capsys):
    """convbin/rnx2rtkp print their own per-epoch progress to stderr with
    no newlines at all (meant to overwrite the same terminal line) -
    piped into the add-on's own logs unchanged, that turns into
    thousands of lines for a multi-hour file. _run_quiet() captures it
    instead of letting it reach the add-on's own stdout/stderr, but still
    surfaces it (so a real failure - e.g. the "invalid option value"
    config bug found this way in production - isn't silently harder to
    diagnose)."""
    def fake_run_ok(cmd, capture_output, text):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="lots of progress spam")

    monkeypatch.setattr(ppp.subprocess, "run", fake_run_ok)
    ppp._run_quiet(["some-tool"])
    assert capsys.readouterr().out == "", "must not leak the tool's own output on success"

    def fake_run_fail(cmd, capture_output, text):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="invalid option value foo")

    monkeypatch.setattr(ppp.subprocess, "run", fake_run_fail)
    with pytest.raises(RuntimeError, match="invalid option value foo"):
        ppp._run_quiet(["some-tool"])


def test_run_quiet_lets_output_through_live_when_debug_is_set(monkeypatch):
    """ppp.DEBUG (set by main.py from the "debug" add-on option) is the
    escape hatch for the quieting in the test above - e.g. to inspect a
    tool issue that doesn't raise (rnx2rtkp can exit 0 despite an
    internal problem, see 0.2.33's changelog entry)."""
    monkeypatch.setattr(ppp, "DEBUG", True)
    calls = []
    monkeypatch.setattr(ppp.subprocess, "run", lambda cmd, check: calls.append((cmd, check)))

    ppp._run_quiet(["some-tool", "-x"])

    assert calls == [(["some-tool", "-x"], True)], \
        "must run with check=True and no output capturing, same as before _run_quiet existed"


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


def test_parse_sky_stat_extracts_only_slip_flagged_events(tmp_path):
    """Regression basis: a real installation's poorly-placed antenna was
    diagnosed by correlating RTKLIB's own per-epoch cycle-slip flag (not
    the cumulative slipc counter, which would double-count) with each
    satellite's azimuth/elevation from the same $SAT line - this is the
    parsing half of that same technique, reused for the skyplot page's
    on-demand heatmap."""
    stat = tmp_path / "sky.pos.stat"
    stat.write_text(
        "$POS,2434,206119.000,5,4456368.6,859858.1,4466423.1,0,0,0\n"
        # slip=1 (12th field after $SAT): must be included
        "$SAT,2434,206119.000,G11,1,246.0,35.0,-1.2,0.0,1,40.0,0,1,3,2,5,0\n"
        # same epoch, slip=0: must be excluded, but still counts the epoch once
        "$SAT,2434,206119.000,G09,1,10.0,68.0,0.1,0.0,1,45.0,0,0,4,0,0,0\n"
        # a second epoch, a second slip at a different satellite/position
        "$SAT,2434,206120.000,G19,1,250.0,18.0,2.1,0.0,1,30.0,0,1,1,3,4,1\n"
    )
    result = ppp.parse_sky_stat(stat)
    assert result["epochs"] == 2, "distinct (week, tow) pairs, not $SAT line count"
    assert result["slip_events"] == [
        {"sat": "G11", "azimuth": 246.0, "elevation": 35.0},
        {"sat": "G19", "azimuth": 250.0, "elevation": 18.0},
    ]


def test_parse_sky_stat_returns_empty_when_no_slips(tmp_path):
    stat = tmp_path / "sky.pos.stat"
    stat.write_text("$SAT,2434,206119.000,G09,1,10.0,68.0,0.1,0.0,1,45.0,0,0,4,0,0,0\n")
    result = ppp.parse_sky_stat(stat)
    assert result == {"slip_events": [], "epochs": 1}


def test_run_sky_analysis_invokes_rnx2rtkp_with_no_precise_products(monkeypatch, tmp_path):
    """Regression: a first version of this used pos1-posmode=single, which
    seemed like the obvious lightweight choice but turned out to never
    detect any cycle slip at all - RTKLIB only runs slip detection from
    the PPP/RTK code path, never from plain single-point positioning.
    ppp-static (with dynamics=on and out-outsingle=on, see
    SKY_ANALYSIS_CONF's own comment) is required to actually get slip
    data - verified against a real installation's raw log that it still
    works with no SP3/CLK/ANTEX arguments at all, just obs+nav."""
    calls = []

    def fake_run(cmd, capture_output, text):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ppp.subprocess, "run", fake_run)

    stat_path = ppp.run_sky_analysis("obs.o", "nav.n", tmp_path)

    assert stat_path == tmp_path / "sky.pos.stat"
    assert calls[0][:2] == ["rnx2rtkp", "-k"]
    assert "-y" in calls[0] and calls[0][calls[0].index("-y") + 1] == "2"
    assert calls[0][-2:] == ["obs.o", "nav.n"], "no SP3/CLK/ANTEX arguments needed"
    conf_text = (tmp_path / "sky.conf").read_text()
    assert "pos1-posmode       =ppp-static" in conf_text
    assert "pos1-dynamics      =on" in conf_text
    assert "out-outsingle      =on" in conf_text
