"""``build_context(job)``: load the job's exact inputs and build a fresh model context.

The worker selects records by the job's own references (never "the newest result") and reads
the config saved on the job's run. Each stage then picks only the fields its role may see. The
exact messages sent to a model are saved with every call in ``provider_calls``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from gtm.config import ResolvedConfig, parse_stored
from gtm.contracts import LeadRecord
from gtm.db import Conn, maybe_one
from gtm.queue import Job
from gtm.util import canonical_json

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
    content: dict[str, Any]


@dataclass(frozen=True)
class StageInputs:
    job: Job
    config: ResolvedConfig
    record: LeadRecord
    input_result: InputResult | None
    today: date


@dataclass(frozen=True)
class StageContext:
    inputs: StageInputs
    payload: dict[str, Any]


def load_inputs(conn: Conn, job: Job, today: date) -> StageInputs:
    run = maybe_one(conn, "SELECT config FROM gtm.runs WHERE id = %s", (job.run_id,))
    rec = maybe_one(
        conn,
        "SELECT schema_version, content FROM gtm.lead_records WHERE id = %s AND lead_id = %s",
        (job.lead_record_id, job.lead_id),
    )
    if run is None or rec is None:
        raise ContextError("missing_parent")
    if rec["schema_version"] != "lead_record.v1":
        raise ContextError("unsupported_schema")
    try:
        config = parse_stored(run["config"])
        record = LeadRecord.model_validate(rec["content"])
    except ValidationError as exc:
        raise ContextError("unsupported_schema") from exc

    input_result = None
    if job.stage in EXPECTED_INPUT:
        row = maybe_one(
            conn,
            """SELECT id, kind, version, schema_version, content FROM gtm.stage_results
                WHERE id = %s AND lead_id = %s AND lead_record_id = %s""",
            (job.input_result_id, job.lead_id, job.lead_record_id),
        )
        if row is None:
            raise ContextError("missing_parent")
        if (row["kind"], row["schema_version"]) != EXPECTED_INPUT[job.stage]:
            raise ContextError("unsupported_schema")
        input_result = InputResult(row["id"], row["kind"], row["version"], row["content"])
    return StageInputs(job, config, record, input_result, today)


def build_context(
    conn: Conn, job: Job, today: date, build_payload: Callable[[StageInputs], dict[str, Any]]
) -> StageContext:
    inputs = load_inputs(conn, job, today)
    # Round-trip through JSON so the payload holds exactly what gets stored with each call.
    return StageContext(inputs, json.loads(canonical_json(build_payload(inputs))))
