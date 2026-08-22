"""
Backup su disco della posizione fissa calcolata (survey-in, campagna PPP,
o inserimento manuale), con provenienza: metodo, data/ora, e parametri
usati. Serve a non perdere questa informazione se il container/add-on
viene ricreato — il ricevitore potrebbe aver salvato la posizione nella
propria configurazione interna (se il comando "saveconfig" è andato a
buon fine), ma qui teniamo una copia indipendente e leggibile, con il
"perché" della posizione, non solo il valore.
"""

import datetime as dt
import json
from pathlib import Path

DEFAULT_PATH = Path("/data/position_backup.json")


def save(lat, lon, height, method, receiver_type, path=None, **extra):
    """method: 'survey_in' | 'ppp' | 'manual'. extra: metadati specifici
    del metodo (es. num_samples, duration_hours) da conservare per contesto."""
    data = {
        "lat": lat,
        "lon": lon,
        "height": height,
        "method": method,
        "receiver_type": receiver_type,
        "computed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        **extra,
    }
    # path=None (non DEFAULT_PATH) come default: così un monkeypatch di
    # position_backup.DEFAULT_PATH nei test ha effetto anche su chiamate
    # che non specificano il path esplicitamente.
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
