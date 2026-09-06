"""Regression: the Dockerfile COPYs top-level .py modules by an explicit
list (not the whole directory), so a real deployment once crashed
outright with ModuleNotFoundError - mqtt_shadow.py existed, was imported
by main.py, and had passing tests, but was never added to that list, so
it was simply missing from the built image. This check catches that
class of mistake without needing an actual Docker build."""

import re
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent


def test_dockerfile_copies_every_top_level_python_module():
    dockerfile = (BASE_DIR / "Dockerfile").read_text()
    m = re.search(r"^COPY ((?:\S+\.py ?)+)/app/$", dockerfile, re.MULTILINE)
    assert m, "expected a `COPY *.py /app/` line in the Dockerfile"
    copied = set(m.group(1).split())

    actual = {p.name for p in BASE_DIR.glob("*.py")}

    assert copied == actual, (
        f"Dockerfile COPY list is out of sync with the actual .py files in "
        f"{BASE_DIR.name}/ - missing from Dockerfile: {actual - copied}, "
        f"listed but no longer present: {copied - actual}"
    )
