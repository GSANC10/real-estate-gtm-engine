"""``build_context(job)``: load the job's exact pinned inputs and save the model context.

The worker selects records by the job's own references (never "the newest result"), checks
versions, schemas, and prompt hashes, and stores the exact application-built model messages as
a private ``context_snapshots`` row. A retry of the same job reuses that snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from gtm.config import TOOL_SCHEMA_VERSION, ResolvedConfig, parse_stored
from gtm.contracts import LeadRecord
from gtm.db import Conn, all_rows, maybe_one, one, tenant_tx
from gtm.queue import Job, assert_owned
from gtm.util import canonical_json, json_sha256

ROLE_FOR_STAGE = {"research": "researcher", "check": "claim_check", "write": "writer"}
EXPECTED_INPUT = {
    "check": ("research", "research.output.v1"),
    "write": ("checked_research", "checked_research.v1"),
}


class ContextError(Exception):
    """The job cannot run as pinned (missing parent, unsupported schema, oversized context)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class InputResult:
    id: UUID
    kind: str
    version: int
    schema_version: str
    content: dict[str, Any]


@dataclass(frozen=True)
class StageInputs:
    job: Job
    config: ResolvedConfig
    record: LeadRecord
    input_result: InputResult | None
    citations: list[dict[str, Any]]
    today: date


@dataclass(frozen=True)
class StageContext:
    inputs: StageInputs
    snapshot_id: UUID
    payload: dict[str, Any]


def load_inputs(conn: Conn, job: Job, today: date) -> StageInputs:
    with tenant_tx(conn, job.tenant_id):
        assert_owned(conn, job)
        cfg_row = maybe_one(
            conn, "SELECT content FROM gtm.config_versions WHERE id = %s", (job.config_version_id,)
        )
        rec_row = maybe_one(
            conn,
            "SELECT schema_version, content FROM gtm.lead_records WHERE id = %s AND lead_id = %s",
            (job.lead_record_id, job.lead_id),
        )
        result_row = None
        if job.input_result_id is not None:
            result_row = maybe_one(
                conn,
                """SELECT id, kind, version, schema_version, content, lead_record_id
                     FROM gtm.stage_results WHERE id = %s AND lead_id = %s""",
                (job.input_result_id, job.lead_id),
            )
        citations: list[dict[str, Any]] = []
        if job.stage == "check" and result_row is not None:
            citations = all_rows(
                conn,
                """SELECT id, key, source, url, quote, source_date, date_basis, identifiers_seen
                     FROM gtm.citations WHERE research_result_id = %s ORDER BY key""",
                (job.input_result_id,),
            )

    if cfg_row is None or rec_row is None:
        raise ContextError("missing_parent")
    if rec_row["schema_version"] != "lead_record.v1":
        raise ContextError("unsupported_schema")
    try:
        config = parse_stored(cfg_row["content"])
        record = LeadRecord.model_validate(rec_row["content"])
    except ValidationError as exc:
        raise ContextError("unsupported_schema") from exc

    role = ROLE_FOR_STAGE.get(job.stage)
    if role is not None:
        pinned = config.roles.get(role)
        if job.model_id != pinned.model_id or job.prompt_sha256 != config.prompts[role].sha256:
            raise ContextError("pinned_version_mismatch")

    input_result = None
    if job.stage in EXPECTED_INPUT:
        if result_row is None:
            raise ContextError("missing_parent")
        kind, schema = EXPECTED_INPUT[job.stage]
        if result_row["kind"] != kind or result_row["schema_version"] != schema:
            raise ContextError("unsupported_schema")
        if (
            result_row["version"] != job.input_result_version
            or result_row["lead_record_id"] != job.lead_record_id
        ):
            raise ContextError("input_version_mismatch")
        input_result = InputResult(
            result_row["id"],
            result_row["kind"],
            result_row["version"],
            result_row["schema_version"],
            result_row["content"],
        )
    return StageInputs(job, config, record, input_result, citations, today)


def build_context(
    conn: Conn, job: Job, today: date, build_payload: Callable[[StageInputs], dict[str, Any]]
) -> StageContext:
    inputs = load_inputs(conn, job, today)
    with tenant_tx(conn, job.tenant_id):
        existing = maybe_one(
            conn,
            "SELECT id, payload FROM gtm.context_snapshots WHERE job_id = %s AND snapshot_version = 1",
            (job.id,),
        )
    if existing is not None:
        return StageContext(inputs, existing["id"], existing["payload"])

    # Round-trip through JSON so a fresh run and a retry from the stored snapshot see identical data.
    payload = json.loads(canonical_json(build_payload(inputs)))
    role = ROLE_FOR_STAGE[job.stage]
    input_refs = {
        "lead_record_id": str(job.lead_record_id),
        "input_result_id": str(job.input_result_id) if job.input_result_id else None,
        "input_result_version": job.input_result_version,
        "config_version_id": str(job.config_version_id),
    }
    with tenant_tx(conn, job.tenant_id):
        assert_owned(conn, job)
        snapshot_id: UUID = one(
            conn,
            """INSERT INTO gtm.context_snapshots
                 (tenant_id, job_id, snapshot_version, schema_version, role, payload, payload_sha256,
                  prompt_version, prompt_sha256, model_id, tool_schema_version, config_version_id, input_refs)
               VALUES (%s, %s, 1, 'context_snapshot.v1', %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                job.tenant_id,
                job.id,
                role,
                Jsonb(payload),
                json_sha256(payload),
                job.prompt_version,
                job.prompt_sha256,
                job.model_id,
                TOOL_SCHEMA_VERSION if job.stage == "research" else None,
                job.config_version_id,
                Jsonb(input_refs),
            ),
        )["id"]
    return StageContext(inputs, snapshot_id, payload)
