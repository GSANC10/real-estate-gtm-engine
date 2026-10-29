"""Jobs and atomic stage completion.

One program processes one job at a time. ``complete_stage`` is a single transaction: it saves
the immutable result, marks the job complete, inserts the next job (keyed by its input, so it
is never duplicated), and updates the lead's state. No provider call happens inside it, so a
crash can leave a job unfinished but never half-saved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from gtm.db import Conn, maybe_one, one
from gtm.util import json_sha256

STAGES = ("research", "check", "write")


class ConflictingCompletion(Exception):
    """The job already completed with a different output."""


@dataclass(frozen=True)
class Job:
    id: UUID
    run_id: UUID
    lead_id: UUID
    lead_record_id: UUID
    stage: str
    job_key: str
    input_result_id: UUID | None
    outreach_item_id: UUID | None
    rerun: int
    attempt: int
    max_attempts: int


@dataclass(frozen=True)
class StageOutput:
    kind: str
    schema_version: str
    content: dict[str, Any]
    outcome: str | None
    lead_state: str
    lead_state_reason: str | None
    next_stage: str | None

    def sha256(self) -> str:
        return json_sha256(self.__dict__)


@dataclass(frozen=True)
class Completion:
    result_id: UUID
    next_job_id: UUID | None
    replayed: bool


def recover_interrupted(conn: Conn) -> int:
    """At startup, jobs left running by a stopped program go back to the queue."""
    rows = conn.execute(
        "UPDATE gtm.jobs SET status = 'queued', updated_at = now() WHERE status = 'running' RETURNING id"
    ).fetchall()
    return len(rows)


def next_job(conn: Conn, stages: tuple[str, ...] = STAGES) -> Job | None:
    row = maybe_one(
        conn,
        """UPDATE gtm.jobs SET status = 'running', attempts = attempts + 1, updated_at = now()
            WHERE id = (SELECT id FROM gtm.jobs
                         WHERE status = 'queued' AND stage = ANY(%s) AND available_at <= now()
                         ORDER BY seq LIMIT 1)
        RETURNING *""",
        (list(stages),),
    )
    if row is None:
        return None
    return Job(
        id=row["id"],
        run_id=row["run_id"],
        lead_id=row["lead_id"],
        lead_record_id=row["lead_record_id"],
        stage=row["stage"],
        job_key=row["job_key"],
        input_result_id=row["input_result_id"],
        outreach_item_id=row["outreach_item_id"],
        rerun=row["rerun"],
        attempt=row["attempts"],
        max_attempts=row["max_attempts"],
    )


def set_lead_state(conn: Conn, lead_id: UUID, state: str, reason: str | None) -> None:
    conn.execute(
        "UPDATE gtm.leads SET state = %s, state_reason = %s, updated_at = now() WHERE id = %s",
        (state, reason, lead_id),
    )


def finish_job(
    conn: Conn,
    job: Job,
    status: str,
    error_code: str,
    *,
    lead_state: str | None = None,
    retry_after_seconds: int | None = None,
) -> None:
    """End or park a job without a result: failed, cancelled, blocked, or queued for a retry."""
    with conn.transaction():
        conn.execute(
            """UPDATE gtm.jobs
                  SET status = %(status)s, error_code = %(code)s, updated_at = now(),
                      available_at = CASE WHEN %(retry)s::integer IS NULL THEN available_at
                                          ELSE now() + make_interval(secs => %(retry)s::integer) END,
                      completed_at = CASE WHEN %(status)s IN ('failed', 'cancelled') THEN now() END
                WHERE id = %(id)s""",
            {"status": status, "code": error_code, "retry": retry_after_seconds, "id": job.id},
        )
        if lead_state is not None:
            set_lead_state(conn, job.lead_id, lead_state, error_code)


def complete_stage(conn: Conn, job: Job, output: StageOutput) -> Completion:
    output_sha = output.sha256()
    with conn.transaction():
        status = one(conn, "SELECT status FROM gtm.jobs WHERE id = %s FOR UPDATE", (job.id,))[
            "status"
        ]
        if status == "completed":
            return _replay(conn, job, output_sha)

        version = one(
            conn,
            "SELECT coalesce(max(version), 0) + 1 AS v FROM gtm.stage_results WHERE lead_id = %s AND kind = %s",
            (job.lead_id, output.kind),
        )["v"]
        result_id: UUID = one(
            conn,
            """INSERT INTO gtm.stage_results (lead_id, lead_record_id, job_id, kind, schema_version, version,
                                              parent_result_id, outcome, reason, content, output_sha256)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                job.lead_id,
                job.lead_record_id,
                job.id,
                output.kind,
                output.schema_version,
                version,
                job.input_result_id,
                output.outcome,
                output.lead_state_reason,
                Jsonb(output.content),
                output_sha,
            ),
        )["id"]

        next_job_id: UUID | None = None
        reason = output.lead_state_reason
        if output.next_stage == "draft":
            next_job_id, reason = _schedule_draft(conn, job, result_id, reason)
        elif output.next_stage is not None:
            next_job_id = _insert_job(
                conn,
                job,
                output.next_stage,
                f"{output.next_stage}/{result_id}",
                input_result_id=result_id,
            )

        conn.execute(
            """UPDATE gtm.jobs SET status = 'completed', error_code = NULL, completed_at = now(), updated_at = now()
                WHERE id = %s""",
            (job.id,),
        )
        set_lead_state(conn, job.lead_id, output.lead_state, reason)
    return Completion(result_id, next_job_id, replayed=False)


def _replay(conn: Conn, job: Job, output_sha: str) -> Completion:
    existing = maybe_one(
        conn, "SELECT id, output_sha256 FROM gtm.stage_results WHERE job_id = %s", (job.id,)
    )
    if existing is None or existing["output_sha256"] != output_sha:
        raise ConflictingCompletion(str(job.id))
    nxt = maybe_one(
        conn,
        """SELECT j.id FROM gtm.jobs j
            WHERE j.input_result_id = %(r)s
               OR j.outreach_item_id IN (SELECT o.id FROM gtm.outreach_items o WHERE o.writing_result_id = %(r)s)
            LIMIT 1""",
        {"r": existing["id"]},
    )
    return Completion(existing["id"], nxt["id"] if nxt else None, replayed=True)


def _insert_job(
    conn: Conn,
    parent: Job,
    stage: str,
    key: str,
    *,
    input_result_id: UUID,
    outreach_item_id: UUID | None = None,
) -> UUID:
    row = maybe_one(
        conn,
        """INSERT INTO gtm.jobs (run_id, lead_id, lead_record_id, stage, job_key, input_result_id,
                                 outreach_item_id, rerun)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (job_key) DO NOTHING RETURNING id""",
        (
            parent.run_id,
            parent.lead_id,
            parent.lead_record_id,
            stage,
            key,
            input_result_id,
            outreach_item_id,
            parent.rerun,
        ),
    )
    job_id: UUID = (
        row["id"] if row else one(conn, "SELECT id FROM gtm.jobs WHERE job_key = %s", (key,))["id"]
    )
    return job_id


def _schedule_draft(
    conn: Conn, job: Job, result_id: UUID, reason: str | None
) -> tuple[UUID | None, str | None]:
    """One first-touch outreach item per lead; a later writing version never adds a second draft."""
    created = maybe_one(
        conn,
        """INSERT INTO gtm.outreach_items (lead_id, step, writing_result_id) VALUES (%s, 'first_touch', %s)
           ON CONFLICT (lead_id, step) DO NOTHING RETURNING id""",
        (job.lead_id, result_id),
    )
    if created is None:
        return None, "outreach_item_exists"
    draft_id = _insert_job(
        conn,
        job,
        "draft",
        f"draft/{created['id']}",
        input_result_id=result_id,
        outreach_item_id=created["id"],
    )
    return draft_id, reason
