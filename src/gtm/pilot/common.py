"""Run folders, the cost log, and text/URL/date normalization shared by the pilot stages.

Pilot data lives outside git, one folder per run: ``~/gtm-data/pilot/<date>/<run id>/``. Stages
write new files and refuse to overwrite, so original outputs are always preserved. The one
exception is discovery's summary files (``discovered.json``, ``discovery-summary.json``), which are
rebuilt from the saved raw responses when a run is resumed, and only until verification starts.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from gtm.pilot.models import Criteria
from gtm.util import canonical_json, sha256_hex

DEFAULT_DATA_DIR = Path.home() / "gtm-data" / "pilot"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CRITERIA = REPO_ROOT / "pilot" / "criteria.yaml"


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_criteria(path: Path) -> Criteria:
    return Criteria.model_validate(yaml.safe_load(path.read_text()))


def new_run_dir(base: Path, label: str, now: datetime | None = None) -> Path:
    """``base/2026-10-30/20261030T231500Z-label``: grouped by date, and the run ID is the folder name."""
    if not re.fullmatch(r"[a-z0-9-]{1,40}", label):
        raise ValueError("label must be 1-40 characters of a-z, 0-9, or -")
    at = now or datetime.now(UTC)
    path = base / at.strftime("%Y-%m-%d") / f"{at.strftime('%Y%m%dT%H%M%SZ')}-{label}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_new(path: Path, text: str) -> None:
    if path.exists():
        raise FileExistsError(f"{path} already exists; pilot files are never overwritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def write_new_json(path: Path, value: Any) -> None:
    write_new(path, json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")


def rebuild_json(path: Path, value: Any) -> None:
    """Only for files derived entirely from saved originals (see the module docstring)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def append_cost(run_dir: Path, entry: dict[str, Any]) -> None:
    with (run_dir / "costs.jsonl").open("a") as f:
        f.write(canonical_json(entry) + "\n")


def read_costs(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "costs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# Curly quotes and en/em dashes, written as escapes so the source stays plain ASCII.
_QUOTES = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"}
)


def norm_text(text: str) -> str:
    """Compare text the way a reader would: case, accents, curly quotes, and spacing don't matter."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return re.sub(r"\s+", " ", text).strip().casefold()


def norm_url(url: str) -> str:
    url = url.strip().split("#", 1)[0]
    return re.sub(r"^https?://(www\.)?", "", url, flags=re.I).rstrip("/").casefold()


def opportunity_id(address: str | None, city: str, first_url: str) -> str:
    """Stable ID: by street address and city when known, otherwise by the main source URL."""
    basis = f"{norm_text(address)}|{norm_text(city)}" if address else norm_url(first_url)
    return "opp-" + sha256_hex(basis)[:12]


_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y", "%m/%d/%Y", "%d %B %Y", "%Y/%m/%d")


def parse_date(value: str | None) -> date | None:
    """Parse ISO dates (with or without a time) and common written formats; None if unsure."""
    if not value:
        return None
    value = value.strip()
    iso = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None
