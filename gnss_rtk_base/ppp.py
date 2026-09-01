"""
PPP-static processing (porting of ppp_process.py into the add-on).

Downloads the precise IGS products for the date(s) covered by the raw log
accumulated by str2str, converts it to RINEX with convbin, and runs
rnx2rtkp in PPP-static mode to get the absolute position of the base.
"""

import datetime as dt
import glob
import gzip
import re
import shutil
import subprocess

import requests

GPS_EPOCH = dt.date(1980, 1, 6)

# files.igs.org no longer mirrors IGS products (confirmed against the live
# server: its own /pub/product/readme.txt says so and points elsewhere) -
# using it here would have made every download fail. BKG (Bundesamt fuer
# Kartographie und Geodaesie, Germany) is public and requires no
# authentication, verified with a real download; CDDIS is kept as a
# fallback for setups with NASA Earthdata credentials in ~/.netrc (see the
# note in ISTRUZIONI.md), but 401s otherwise.
SP3_CLK_MIRRORS = [
    "https://igs.bkg.bund.de/root_ftp/IGS/products/{week}",
    "https://cddis.nasa.gov/archive/gnss/products/{week}",
]
ANTEX_MIRRORS = [
    "https://igs.bkg.bund.de/root_ftp/IGS/station/general/igs20.atx",
    "https://cddis.nasa.gov/archive/gnss/data/daily/misc/igs20.atx",
]

PPP_CONF_TEMPLATE = """\
pos1-posmode       =ppp-static
pos1-frequency     =l1+l2
pos1-soltype       =forward
pos1-elmask        =10
pos1-ionoopt       =iflc
pos1-tropopt       =est-ztd
pos1-dynamics      =off
pos1-tidecorr      =off
pos1-niter         =1
pos2-armode        =off
pos2-gloarmode     =off
out-solformat      =llh
out-outhead        =on
out-outopt         =on
out-timesys        =gpst
out-height         =ellipsoidal
stats-errphase     =0.003
stats-errphaseel   =0.003
stats-errphasebl   =0
stats-errdoppler   =1
stats-stdbias      =30
stats-stdiono      =0.03
stats-stdtrop      =0.3
stats-prnaccelh    =1
stats-prnaccelv    =1
stats-prnbias      =0.0001
stats-prniono      =0.001
stats-prntrop      =0.0001
stats-clkstab      =5e-12
"""


def gps_week_dow(date):
    delta_days = (date - GPS_EPOCH).days
    return delta_days // 7, delta_days % 7


def collect_raw_files(raw_log_dir, start_ts, end_ts):
    """Selects the gnssbase_YYYYMMDDHH.rtcm3 files that cover
    [start_ts, end_ts], with a one-hour margin on both sides to avoid
    edge cutoffs."""
    pattern = re.compile(r"gnssbase_(\d{4})(\d{2})(\d{2})(\d{2})\.rtcm3$")
    margin = 3600
    selected = []
    for path in sorted(glob.glob(f"{raw_log_dir}/gnssbase_*.rtcm3")):
        m = pattern.search(path)
        if not m:
            continue
        y, mo, d, h = (int(x) for x in m.groups())
        file_ts = dt.datetime(y, mo, d, h, tzinfo=dt.timezone.utc).timestamp()
        if start_ts - margin <= file_ts <= end_ts + margin:
            selected.append(path)
    return selected


def concat_raw_files(paths, out_path):
    with open(out_path, "wb") as out:
        for p in paths:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out)


def convbin(raw_path, workdir):
    obs_path = f"{workdir}/campaign.obs"
    nav_path = f"{workdir}/campaign.nav"
    subprocess.run(["convbin", raw_path, "-r", "rtcm3", "-o", obs_path, "-n", nav_path], check=True)
    return obs_path, nav_path


def parse_obs_dates(obs_path):
    """Returns the set of UTC dates covered by the RINEX file (usually
    one, but can be more if the campaign spans midnight)."""
    dates = set()
    with open(obs_path, "r", errors="ignore") as f:
        for line in f:
            if "TIME OF FIRST OBS" in line or "TIME OF LAST OBS" in line:
                nums = re.findall(r"-?\d+\.?\d*", line)
                y, mo, d = int(nums[0]), int(nums[1]), int(nums[2])
                dates.add(dt.date(y, mo, d))
            if line.startswith(">"):
                break
    if not dates:
        raise ValueError(f"Cannot find TIME OF FIRST/LAST OBS in {obs_path}")
    return sorted(dates)


def try_download(urls, dest):
    """Downloads the first URL that actually returns the real file.

    A 200 status with a long enough body isn't proof of success: a mirror
    requiring authentication (e.g. CDDIS without NASA Earthdata
    credentials) can serve an HTML login/error page with a 200 status,
    long enough to pass a bare length check - found in production, where
    this made a PPP campaign fail deep inside gzip decompression with a
    confusing "Not a gzipped file (b'<!')" instead of a clear "download
    failed" error. Rejects anything that looks like HTML instead of the
    expected binary/text product file."""
    for url in urls:
        try:
            r = requests.get(url, timeout=60)
            looks_like_html = (r.content.lstrip()[:1] == b"<"
                                or "html" in r.headers.get("Content-Type", "").lower())
            if r.status_code == 200 and len(r.content) > 1000 and not looks_like_html:
                dest.write_bytes(r.content)
                return True
        except requests.RequestException:
            continue
    return False


def gunzip(path):
    out = path.with_suffix("")
    with gzip.open(path, "rb") as fin, open(out, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    return out


def build_igs_names(date, product):
    """Verified against a real directory listing (BKG mirror): FIN only
    publishes 15M-sampled orbits (not 05M) and RAP only publishes
    05M-sampled clocks (not 30S) - only FIN has a 30S clock variant."""
    doy = date.timetuple().tm_yday
    tag = {"FIN": "IGS0OPSFIN", "RAP": "IGS0OPSRAP"}[product]
    clk_sample = "30S" if product == "FIN" else "05M"
    sp3 = f"{tag}_{date.year}{doy:03d}0000_01D_15M_ORB.SP3.gz"
    clk = f"{tag}_{date.year}{doy:03d}0000_01D_{clk_sample}_CLK.CLK.gz"
    return sp3, clk


def fetch_precise_products(dates, workdir, products=("FIN", "RAP")):
    """Downloads SP3+CLK for each covered date, and the ANTEX file (once,
    shared cache). Returns (sp3_list, clk_list, atx_path, tiers_used),
    where tiers_used is {date: product} recording which tier actually
    succeeded for each date - used by the PPP campaign to decide whether
    a later refinement with "final" products is worth attempting (no
    point if every date already used "FIN").

    Tries each product tier in order (final first, then rapid) and uses
    the first one where both SP3 and CLK download successfully. This
    matters for the automatic PPP campaign (run_ppp_campaign): it
    processes the raw log right after the logging window ends, when IGS
    "final" products for that date are essentially never published yet
    (published ~11-18 days later) - rapid products (~17-41h latency) are
    the realistic fallback for a same-day/next-day campaign. Without
    this, the automatic campaign would fail every time by default."""
    sp3_paths, clk_paths, tiers_used = [], [], {}
    for date in dates:
        week, _ = gps_week_dow(date)
        sp3_gz = clk_gz = None
        for product in products:
            sp3_name, clk_name = build_igs_names(date, product)
            candidate_sp3 = workdir / sp3_name
            candidate_clk = workdir / clk_name
            sp3_urls = [f"{m.format(week=week)}/{sp3_name}" for m in SP3_CLK_MIRRORS]
            clk_urls = [f"{m.format(week=week)}/{clk_name}" for m in SP3_CLK_MIRRORS]
            if try_download(sp3_urls, candidate_sp3) and try_download(clk_urls, candidate_clk):
                sp3_gz, clk_gz = candidate_sp3, candidate_clk
                tiers_used[date] = product
                break
            for stale in (candidate_sp3, candidate_clk):
                stale.unlink(missing_ok=True)
        if sp3_gz is None:
            raise RuntimeError(
                f"No IGS products available for {date} (tried: {', '.join(products)})")
        sp3_paths.append(gunzip(sp3_gz))
        clk_paths.append(gunzip(clk_gz))

    atx_path = workdir / "igs20.atx"
    if not atx_path.exists():
        if not try_download(ANTEX_MIRRORS, atx_path):
            raise RuntimeError("igs20.atx download failed")
    return sp3_paths, clk_paths, atx_path, tiers_used


def run_rnx2rtkp(obs_path, nav_path, sp3_paths, clk_paths, atx_path, workdir):
    conf_path = workdir / "ppp.conf"
    conf_path.write_text(PPP_CONF_TEMPLATE)
    out_pos = workdir / "result.pos"
    cmd = ["rnx2rtkp", "-k", str(conf_path), "-o", str(out_pos), str(obs_path), str(nav_path)]
    cmd += [str(p) for p in sp3_paths] + [str(p) for p in clk_paths] + [str(atx_path)]
    subprocess.run(cmd, check=True)
    return out_pos


def parse_last_position(pos_path):
    """Returns (lat, lon, height) of the last valid epoch in the .pos file."""
    last = None
    with open(pos_path, "r", errors="ignore") as f:
        for line in f:
            if line.startswith("%") or not line.strip():
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                lat, lon, height = float(parts[2]), float(parts[3]), float(parts[4])
            except ValueError:
                continue
            last = (lat, lon, height)
    if last is None:
        raise ValueError(f"No valid epoch found in {pos_path}")
    return last
