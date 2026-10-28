"""Private operator import: CSV rows become immutable lead record versions in the database.

CSV is an interchange format only. Rows are validated, deduplicated by the lead identity key,
and checked against suppressions. A suppressed contact's details are not stored. Re-importing an
unchanged row reuses its record version; a changed row adds a new version.
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
from gtm.db import Conn, maybe_one, one, tenant_tx
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
    lead_ids: list[UUID] = field(default_factory=list)


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


def import_contacts(
    conn: Conn, tenant_id: UUID, campaign_id: UUID, path: Path, source_label: str
) -> ImportSummary:
    raw = path.read_bytes()
    reader = csv.DictReader(raw.decode("utf-8-sig").splitlines())
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"CSV is missing columns: {sorted(missing)}")
    rows = list(reader)

    with tenant_tx(conn, tenant_id):
        if maybe_one(conn, "SELECT 1 FROM gtm.campaigns WHERE id = %s", (campaign_id,)) is None:
            raise LookupError("campaign not found for this tenant")
        import_id: UUID = one(
            conn,
            """INSERT INTO gtm.contact_imports (tenant_id, campaign_id, source_label, file_sha256, row_count)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (tenant_id, campaign_id, source_label, hashlib.sha256(raw).hexdigest(), len(rows)),
        )["id"]
        summary = ImportSummary(import_id, len(rows))
        seen: set[str] = set()
        for number, row in enumerate(rows, start=1):
            outcome, error, lead_id, record_id = _import_row(
                conn, tenant_id, campaign_id, row, seen
            )
            conn.execute(
                """INSERT INTO gtm.import_rows (tenant_id, import_id, row_number, outcome, lead_id, lead_record_id,
                                               error_code)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (tenant_id, import_id, number, outcome, lead_id, record_id, error),
            )
            summary.outcomes[outcome] = summary.outcomes.get(outcome, 0) + 1
            if lead_id is not None:
                summary.lead_ids.append(lead_id)
    return summary


def _import_row(
    conn: Conn, tenant_id: UUID, campaign_id: UUID, row: dict[str, str], seen: set[str]
) -> tuple[str, str | None, UUID | None, UUID | None]:
    try:
        record = parse_row(row)
    except (ValidationError, ValueError, KeyError):
        return "invalid", "invalid_row", None, None
    if "@" not in record.email:
        return "invalid", "invalid_email", None, None
    identity = email_identity_key(record.email)
    if identity in seen:
        return "duplicate_in_file", None, None, None
    seen.add(identity)
    suppressed = maybe_one(
        conn, "SELECT 1 FROM gtm.suppressions WHERE kind = 'email' AND value_hash = %s", (identity,)
    )
    if suppressed is not None:
        return "suppressed", None, None, None

    conn.execute(
        "INSERT INTO gtm.leads (tenant_id, identity_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (tenant_id, identity),
    )
    lead_id: UUID = one(conn, "SELECT id FROM gtm.leads WHERE identity_key = %s", (identity,))["id"]
    content: dict[str, Any] = record.model_dump(mode="json")
    sha = json_sha256(content)
    existing = maybe_one(
        conn,
        "SELECT id FROM gtm.lead_records WHERE lead_id = %s AND content_sha256 = %s",
        (lead_id, sha),
    )
    if existing is not None:
        record_id: UUID = existing["id"]
    else:
        version = one(
            conn,
            "SELECT coalesce(max(version), 0) + 1 AS v FROM gtm.lead_records WHERE lead_id = %s",
            (lead_id,),
        )["v"]
        record_id = one(
            conn,
            """INSERT INTO gtm.lead_records (tenant_id, lead_id, version, schema_version, content, content_sha256)
               VALUES (%s, %s, %s, 'lead_record.v1', %s, %s) RETURNING id""",
            (tenant_id, lead_id, version, Jsonb(content), sha),
        )["id"]
    conn.execute(
        """INSERT INTO gtm.campaign_leads (tenant_id, campaign_id, lead_id) VALUES (%s, %s, %s)
           ON CONFLICT DO NOTHING""",
        (tenant_id, campaign_id, lead_id),
    )
    return "imported", None, lead_id, record_id
