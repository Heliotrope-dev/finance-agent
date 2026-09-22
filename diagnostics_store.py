"""Dependency-free reader for the offline score-diagnostics artifact.

The calculation module imports SciPy.  The Streamlit process on the 1.9G host
must not import it, so the scheduled job writes JSON and the UI reads only that.
"""

from __future__ import annotations

import json
from pathlib import Path


_REPORT_PATH = Path(__file__).resolve().parent / "data" / "score_diagnostics.json"


def load_score_diagnostics(path: Path | None = None) -> dict:
    try:
        return json.loads((path or _REPORT_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
