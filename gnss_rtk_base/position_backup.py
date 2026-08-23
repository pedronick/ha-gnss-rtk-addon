"""
On-disk backup of the computed fixed position (survey-in, PPP campaign,
or manual entry), with provenance: method, timestamp, and parameters
used. This avoids losing this information if the container/add-on gets
recreated — the receiver may have saved the position in its own internal
configuration (if the "saveconfig" command succeeded), but here we keep
an independent, human-readable copy including the "why" of the position,
not just the value.
"""

import datetime as dt
import json
from pathlib import Path

DEFAULT_PATH = Path("/data/position_backup.json")


def save(lat, lon, height, method, receiver_type, path=None, **extra):
    """method: 'survey_in' | 'ppp' | 'manual'. extra: method-specific
    metadata (e.g. num_samples, duration_hours) kept for context."""
    data = {
        "lat": lat,
        "lon": lon,
        "height": height,
        "method": method,
        "receiver_type": receiver_type,
        "computed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        **extra,
    }
    # path=None (not DEFAULT_PATH) as the default: this way a monkeypatch
    # of position_backup.DEFAULT_PATH in tests also affects calls that
    # don't specify the path explicitly.
    Path(path if path is not None else DEFAULT_PATH).write_text(json.dumps(data, indent=2))
    return data


def load(path=None):
    path = Path(path if path is not None else DEFAULT_PATH)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
