"""Public-safety scan for this public repository.

Fails when tracked files contain:
- an email address outside reserved example domains,
- brokerage configuration or contact data outside config/examples/,
- an example configuration not marked fictional,
- a term from a private denylist (GTM_PRIVATE_TERMS_FILE: one term per line, kept off GitHub),
- a secret found by gitleaks, when gitleaks is installed (CI always runs it).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")
ALLOWED_EMAIL_DOMAINS = (
    "example",
    "example.com",
    "example.org",
    "example.net",
    "users.noreply.github.com",
)
DATA_SUFFIXES = {".yaml", ".yml", ".csv"}
# pilot/ holds discovery criteria only (areas, signal types, search tasks), never personal data.
DATA_ALLOWED_PREFIXES = ("config/examples/", ".github/workflows/", "pilot/")


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [ROOT / line for line in out.splitlines() if line and (ROOT / line).is_file()]


def _allowed_domain(domain: str) -> bool:
    d = domain.lower()
    return any(d == a or d.endswith("." + a) for a in ALLOWED_EMAIL_DOMAINS)


def main() -> int:
    problems: list[str] = []
    terms: list[str] = []
    terms_file = os.environ.get("GTM_PRIVATE_TERMS_FILE")
    if terms_file:
        terms = [
            t.strip().lower()
            for t in Path(terms_file).read_text().splitlines()
            if t.strip() and not t.startswith("#")
        ]

    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if path.suffix in DATA_SUFFIXES and not rel.startswith(DATA_ALLOWED_PREFIXES):
            problems.append(
                f"{rel}: configuration or contact data belongs only in config/examples/"
            )
        if rel.startswith("config/") and not rel.startswith("config/examples/"):
            problems.append(f"{rel}: only fictional examples may be committed under config/")
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for m in EMAIL_RE.finditer(text):
            if not _allowed_domain(m.group(1)):
                problems.append(
                    f"{rel}: email address outside reserved example domains: {m.group(0)}"
                )
        if (
            rel.startswith("config/examples/")
            and path.suffix in {".yaml", ".yml"}
            and "kind: fictional" not in text
        ):
            problems.append(f"{rel}: example tenants must be marked 'kind: fictional'")
        low = text.lower()
        problems.extend(f"{rel}: contains a private term" for t in terms if t in low)

    gitleaks = shutil.which("gitleaks")
    if gitleaks:
        result = subprocess.run(
            [gitleaks, "dir", "--redact", "--no-banner", str(ROOT)], capture_output=True, text=True
        )
        if result.returncode != 0:
            problems.append("gitleaks reported findings:\n" + result.stdout + result.stderr)
    else:
        print("gitleaks not installed locally; skipped (CI runs it)")

    for p in problems:
        print(f"FAIL {p}")
    print(
        "public-safety scan: "
        + ("FAILED" if problems else "passed")
        + (f" ({len(terms)} private terms checked)" if terms else "")
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
