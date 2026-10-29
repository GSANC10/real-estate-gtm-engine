"""Contact import: CSV rows become versioned lead records on the master list.

CSV is an interchange format only. Rows are validated, deduplicated by normalized email, and
checked against the do-not-contact list; a suppressed contact's details are not stored.
Re-importing an unchanged row reuses its record; a changed row adds a new version.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from gtm.contracts import LeadRecord, RecordedNeed
from gtm.db import Conn, maybe_one, one
from gtm.util import email_identity_key, json_sha256, normalize_email

REQUIRED_COLUMNS = {
    "email",
    "full_name",
    "organization",
    "residence_location",
    "language_preference",
    "need_category",
    "need_note",
    "property_location",
    "recorded_at",
    "source",
    "may_mention_inquiry",
}


@dataclass
class ImportSummary:
    import_id: UUID
    rows: int
    outcomes: dict[str, int] = field(default_factory=dict)


def _blank(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def parse_row(row: dict[str, str]) -> LeadRecord:
    flag = (row.get("may_mention_inquiry") or "").strip().lower()
    if flag not in {"true", "false"}:
        raise ValueError("may_mention_inquiry must be true or false")
    return LeadRecord(
        email=normalize_email(row["email"]),
        full_name=row["full_name"].strip(),
        organization=_blank(row.get("organization")),
        residence_location=_blank(row.get("residence_location")),
        language_preference=_blank(row.get("language_preference")),
        recorded_need=RecordedNeed(
            category=row["need_category"].strip(),
            note=row["need_note"].strip(),
            property_location=_blank(row.get("property_location")),
            recorded_at=row["recorded_at"].strip(),
            source=row["source"].strip(),
        ),
        may_mention_inquiry=flag == "true",
    )


def import_contacts(conn: Conn, path: Path, source_label: str) -> ImportSummary:
    raw = path.read_bytes()
    reader = csv.DictReader(raw.decode("utf-8-sig").splitlines())
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"CSV is missing columns: {sorted(missing)}")
    rows = list(reader)

    with conn.transaction():
        import_id: UUID = one(
            conn,
            """INSERT INTO gtm.imports (source_label, file_sha256, row_count, outcomes)
               VALUES (%s, %s, %s, '{}') RETURNING id""",
            (source_label, hashlib.sha256(raw).hexdigest(), len(rows)),
        )["id"]
        summary = ImportSummary(import_id, len(rows))
        seen: set[str] = set()
        for row in rows:
            outcome = _import_row(conn, import_id, row, seen)
            summary.outcomes[outcome] = summary.outcomes.get(outcome, 0) + 1
        conn.execute(
            "UPDATE gtm.imports SET outcomes = %s WHERE id = %s",
            (Jsonb(summary.outcomes), import_id),
        )
    return summary


def _import_row(conn: Conn, import_id: UUID, row: dict[str, str], seen: set[str]) -> str:
    try:
        record = parse_row(row)
    except (ValidationError, ValueError, KeyError):
        return "invalid"
    if "@" not in record.email:
        return "invalid"
    identity = email_identity_key(record.email)
    if identity in seen:
        return "duplicate_in_file"
    seen.add(identity)
    if maybe_one(conn, "SELECT 1 FROM gtm.suppressions WHERE value_hash = %s", (identity,)):
        return "suppressed"

    conn.execute(
        "INSERT INTO gtm.leads (identity_key) VALUES (%s) ON CONFLICT DO NOTHING", (identity,)
    )
    lead_id: UUID = one(conn, "SELECT id FROM gtm.leads WHERE identity_key = %s", (identity,))["id"]
    content: dict[str, Any] = record.model_dump(mode="json")
    sha = json_sha256(content)
    if maybe_one(
        conn,
        "SELECT 1 FROM gtm.lead_records WHERE lead_id = %s AND content_sha256 = %s",
        (lead_id, sha),
    ):
        return "unchanged"
    version = one(
        conn,
        "SELECT coalesce(max(version), 0) + 1 AS v FROM gtm.lead_records WHERE lead_id = %s",
        (lead_id,),
    )["v"]
    conn.execute(
        """INSERT INTO gtm.lead_records (lead_id, version, schema_version, content, content_sha256, import_id)
           VALUES (%s, %s, 'lead_record.v1', %s, %s, %s)""",
        (lead_id, version, Jsonb(content), sha, import_id),
    )
    return "imported" if version == 1 else "updated"
