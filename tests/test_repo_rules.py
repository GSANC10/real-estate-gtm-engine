"""Repository rules: no send path, contracts in sync, public-safety scan passes."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from gtm.cli import PILOT_SCHEMAS, export_contracts
from gtm.pilot.models import EXPORTED as PILOT_EXPORTED

ROOT = Path(__file__).resolve().parents[1]
SEND_PATTERNS = re.compile(
    r"messages\.send|drafts\.send|/messages/send|/drafts/send|users\.messages\.send", re.I
)


def test_no_send_path_exists_in_code() -> None:
    hits = [str(p) for p in (ROOT / "src").rglob("*.py") if SEND_PATTERNS.search(p.read_text())]
    assert hits == []


def test_published_contract_schemas_match_the_code(tmp_path: Path) -> None:
    pairs = [(g, ROOT / "docs" / "contracts") for g in export_contracts(tmp_path / "core")]
    pairs += [(g, PILOT_SCHEMAS) for g in export_contracts(tmp_path / "pilot", PILOT_EXPORTED)]
    for generated, folder in pairs:
        published = folder / generated.name
        assert published.exists(), f"run: uv run gtm contracts export ({generated.name} missing)"
        assert json.loads(published.read_text()) == json.loads(generated.read_text()), (
            generated.name
        )


def test_public_safety_scan_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_public_safety.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
