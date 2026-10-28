"""Job claiming, leases, and atomic stage completion.

``claim_job`` leases one eligible job through the hardened ``gtm.claim_job`` function and
returns a fencing token. ``complete_stage`` is one transaction: it verifies the token and lease,
saves the immutable result and its evidence rows, marks the job complete, and inserts the next
job under its input-based key. No provider call ever happens inside that transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from gtm.db import Conn, maybe_one, one, tenant_tx
from gtm.faults import NO_FAULTS, Faults
from gtm.util import json_sha256

M1_STAGES = ("research", "check", "write")
NEXT_ROLE = {"check": "claim_check", "write": "writer"}


class StaleLease(Exception):
    """This worker no longer owns the job (token changed, lease expired, or job finished)."""


class ConflictingCompletion(Exception):
    """The job already completed with a different output."""


@dataclass(frozen=True)
class Claim:
    job_id: UUID
    tenant_id: UUID
    stage: str
    attempt: int
    fencing_token: int


@dataclass(frozen=True)
class Job:
    id: UUID
    tenant_id: UUID
    campaign_id: UUID
    run_id: UUID
    lead_id: UUID
    lead_record_id: UUID
    stage: str
    job_key: str
    input_result_id: UUID | None
    input_result_version: int | None
    outreach_item_id: UUID | None
    config_version_id: UUID
    prompt_version: str | None
    prompt_sha256: str | None
    model_id: str | None
    rerun: int
    attempt: int
    max_attempts: int
    fencing_token: int


@dataclass(frozen=True)
class StageOutput:
    kind: str
    schema_version: str
    content: dict[str, Any]
    outcome: str | None
    lead_state: str
    lead_state_reason: str | None
    next_stage: str | None
    citations: list[dict[str, Any]] = field(default_factory=list)
    citation_checks: list[dict[str, Any]] = field(default_factory=list)
    decision: dict[str, Any] | None = None

    def sha256(self) -> str:
        return json_sha256(
            {
                "kind": self.kind,
                "schema_version": self.schema_version,
                "content": self.content,
                "outcome": self.outcome,
                "lead_state": self.lead_state,
                "lead_state_reason": self.lead_state_reason,
                "next_stage": self.next_stage,
                "citations": self.citations,
                "citation_checks": self.citation_checks,
                "decision": self.decision,
            }
        )


@dataclass(frozen=True)
class Completion:
    result_id: UUID
    next_job_id: UUID | None
    replayed: bool


def claim_job(
    conn: Conn, worker_id: UUID, stages: tuple[str, ...], lease_seconds: int
) -> Claim | None:
    row = maybe_one(
        conn, "SELECT * FROM gtm.claim_job(%s, %s, %s)", (worker_id, list(stages), lease_seconds)
    )
    if row is None:
        return None
    return Claim(
        row["job_id"], row["tenant_id"], row["stage"], row["attempt"], row["fencing_token"]
    )


def load_job(conn: Conn, claim: Claim) -> Job:
    with tenant_tx(conn, claim.tenant_id):
        row = maybe_one(conn, "SELECT * FROM gtm.jobs WHERE id = %s", (claim.job_id,))
    if row is None or row["fencing_token"] != claim.fencing_token or row["status"] != "running":
        raise StaleLease(str(claim.job_id))
    return Job(
        id=row["id"],
        tenant_id=row["tenant_id"],
        campaign_id=row["campaign_id"],
        run_id=row["run_id"],
        lead_id=row["lead_id"],
        lead_record_id=row["lead_record_id"],
        stage=row["stage"],
        job_key=row["job_key"],
        input_result_id=row["input_result_id"],
        input_result_version=row["input_result_version"],
        outreach_item_id=row["outreach_item_id"],
        config_version_id=row["config_version_id"],
        prompt_version=row["prompt_version"],
        prompt_sha256=row["prompt_sha256"],
        model_id=row["model_id"],
        rerun=row["rerun"],
        attempt=row["attempts"],
        max_attempts=row["max_attempts"],
        fencing_token=row["fencing_token"],
    )


def assert_owned(conn: Conn, job: Job) -> None:
    """Inside a tenant transaction: the job is still running under this token and lease."""
    row = maybe_one(
        conn,
        """SELECT status, fencing_token, lease_expires_at > now() AS lease_ok
             FROM gtm.jobs WHERE id = %s FOR UPDATE""",
        (job.id,),
    )
    if (
        row is None
        or row["status"] != "running"
        or row["fencing_token"] != job.fencing_token
        or not row["lease_ok"]
    ):
        raise StaleLease(str(job.id))


def heartbeat(conn: Conn, job: Job, lease_seconds: int) -> None:
    with tenant_tx(conn, job.tenant_id):
        row = maybe_one(
            conn,
            """UPDATE gtm.jobs
                  SET lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
                WHERE id = %s AND status = 'running' AND fencing_token = %s AND lease_expires_at > now()
            RETURNING id""",
            (lease_seconds, job.id, job.fencing_token),
        )
    if row is None:
        raise StaleLease(str(job.id))


def set_lead_state(conn: Conn, job: Job, state: str, reason: str | None) -> None:
    conn.execute(
        """UPDATE gtm.campaign_leads SET state = %s, state_reason = %s, updated_at = now()
            WHERE campaign_id = %s AND lead_id = %s""",
        (state, reason, job.campaign_id, job.lead_id),
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
    """End or park a job without a result: failed, cancelled, blocked, or queued for retry."""
    with tenant_tx(conn, job.tenant_id):
        row = maybe_one(
            conn,
            """UPDATE gtm.jobs
                  SET status = %(status)s, error_code = %(code)s, leased_by = NULL, lease_expires_at = NULL,
                      available_at = CASE WHEN %(retry)s::integer IS NULL THEN available_at
                                          ELSE now() + make_interval(secs => %(retry)s::integer) END,
                      completed_at = CASE WHEN %(status)s IN ('failed', 'cancelled') THEN now() END,
                      updated_at = now()
                WHERE id = %(id)s AND status = 'running' AND fencing_token = %(token)s
            RETURNING id""",
            {
                "status": status,
                "code": error_code,
                "retry": retry_after_seconds,
                "id": job.id,
                "token": job.fencing_token,
            },
        )
        if row is None:
            raise StaleLease(str(job.id))
        if lead_state is not None:
            set_lead_state(conn, job, lead_state, error_code)


def complete_stage(
    conn: Conn,
    job: Job,
    snapshot_id: UUID,
    output: StageOutput,
    faults: Faults = NO_FAULTS,
) -> Completion:
    output_sha = output.sha256()
    with tenant_tx(conn, job.tenant_id):
        state = maybe_one(conn, "SELECT status FROM gtm.jobs WHERE id = %s FOR UPDATE", (job.id,))
        if state is not None and state["status"] == "completed":
            return _replay(conn, job, output_sha)
        assert_owned(conn, job)

        version = one(
            conn,
            """SELECT coalesce(max(version), 0) + 1 AS v FROM gtm.stage_results
                WHERE lead_id = %s AND kind = %s""",
            (job.lead_id, output.kind),
        )["v"]
        result_id: UUID = one(
            conn,
            """INSERT INTO gtm.stage_results
                 (tenant_id, campaign_id, lead_id, lead_record_id, job_id, kind, schema_version, version,
                  parent_result_id, config_version_id, context_snapshot_id, outcome, content, output_sha256)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                job.tenant_id,
                job.campaign_id,
                job.lead_id,
                job.lead_record_id,
                job.id,
                output.kind,
                output.schema_version,
                version,
                job.input_result_id,
                job.config_version_id,
                snapshot_id,
                output.outcome,
                Jsonb(output.content),
                output_sha,
            ),
        )["id"]

        for c in output.citations:
            conn.execute(
                """INSERT INTO gtm.citations (tenant_id, research_result_id, key, source, url, quote,
                                              source_date, date_basis, identifiers_seen)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    job.tenant_id,
                    result_id,
                    c["key"],
                    c["source"],
                    c["url"],
                    c["quote"],
                    c["source_date"],
                    c["date_basis"],
                    Jsonb(c["identifiers_seen"]),
                ),
            )
        for chk in output.citation_checks:
            conn.execute(
                """INSERT INTO gtm.citation_checks (tenant_id, checked_result_id, citation_id, finding_key,
                                                    identity, recency, meaning, method, passed, reasons)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    job.tenant_id,
                    result_id,
                    chk["citation_id"],
                    chk["finding_key"],
                    chk["identity"],
                    chk["recency"],
                    chk["meaning"],
                    Jsonb(chk["method"]),
                    chk["passed"],
                    chk["reasons"],
                ),
            )
        if output.decision is not None:
            d = output.decision
            conn.execute(
                """INSERT INTO gtm.qualification_decisions (tenant_id, campaign_id, lead_id, checked_result_id,
                                                            decision, service_area, property_need, reasons,
                                                            proposed_fit)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    job.tenant_id,
                    job.campaign_id,
                    job.lead_id,
                    result_id,
                    d["decision"],
                    d["service_area"],
                    d["property_need"],
                    d["reasons"],
                    Jsonb(d["proposed_fit"]),
                ),
            )

        next_job_id: UUID | None = None
        lead_reason = output.lead_state_reason
        if output.next_stage == "draft":
            next_job_id, lead_reason = _schedule_draft(conn, job, result_id, version, lead_reason)
        elif output.next_stage is not None:
            next_job_id = _schedule_next(conn, job, output.next_stage, result_id, version)

        conn.execute(
            """UPDATE gtm.jobs SET status = 'completed', error_code = NULL, leased_by = NULL,
                      lease_expires_at = NULL, completed_at = now(), updated_at = now()
                WHERE id = %s""",
            (job.id,),
        )
        set_lead_state(conn, job, output.lead_state, lead_reason)
        faults.hit("complete.before_commit")

    faults.hit("complete.after_commit")
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
            WHERE j.input_result_id = %s
               OR j.outreach_item_id IN (SELECT o.id FROM gtm.outreach_items o WHERE o.writing_result_id = %s)
            LIMIT 1""",
        (existing["id"], existing["id"]),
    )
    return Completion(existing["id"], nxt["id"] if nxt else None, replayed=True)


def _schedule_next(conn: Conn, job: Job, stage: str, result_id: UUID, version: int) -> UUID:
    role = NEXT_ROLE[stage]
    key = one(
        conn,
        "SELECT gtm.job_key(%s, %s, %s, %s) AS k",
        (job.config_version_id, stage, result_id, job.rerun),
    )["k"]
    row = maybe_one(
        conn,
        """INSERT INTO gtm.jobs (tenant_id, campaign_id, run_id, lead_id, lead_record_id, stage, job_key,
                                 input_result_id, input_result_version, config_version_id, prompt_version,
                                 prompt_sha256, model_id, rerun)
           SELECT %(tenant)s, %(campaign)s, %(run)s, %(lead)s, %(record)s, %(stage)s, %(key)s, %(input)s,
                  %(version)s, cv.id, cv.content #>> ARRAY['roles', %(role)s, 'prompt_version'],
                  cv.content #>> ARRAY['prompts', %(role)s, 'sha256'],
                  cv.content #>> ARRAY['roles', %(role)s, 'model_id'], %(rerun)s
             FROM gtm.config_versions cv WHERE cv.id = %(cv)s
           ON CONFLICT (tenant_id, job_key) DO NOTHING
           RETURNING id""",
        {
            "tenant": job.tenant_id,
            "campaign": job.campaign_id,
            "run": job.run_id,
            "lead": job.lead_id,
            "record": job.lead_record_id,
            "stage": stage,
            "key": key,
            "input": result_id,
            "version": version,
            "role": role,
            "rerun": job.rerun,
            "cv": job.config_version_id,
        },
    )
    if row is not None:
        new_id: UUID = row["id"]
        return new_id
    existing: UUID = one(conn, "SELECT id FROM gtm.jobs WHERE job_key = %s", (key,))["id"]
    return existing


def _schedule_draft(
    conn: Conn, job: Job, result_id: UUID, version: int, reason: str | None
) -> tuple[UUID | None, str | None]:
    """One first-touch outreach item per lead and campaign; a later writing version never adds a draft."""
    created = maybe_one(
        conn,
        """INSERT INTO gtm.outreach_items (tenant_id, campaign_id, lead_id, step, writing_result_id)
           VALUES (%s, %s, %s, 'first_touch', %s)
           ON CONFLICT (tenant_id, campaign_id, lead_id, step) DO NOTHING
           RETURNING id""",
        (job.tenant_id, job.campaign_id, job.lead_id, result_id),
    )
    if created is None:
        return None, "outreach_item_exists"
    key = one(
        conn, "SELECT gtm.job_key(%s, 'draft', %s, 0) AS k", (job.config_version_id, created["id"])
    )["k"]
    draft_id: UUID = one(
        conn,
        """INSERT INTO gtm.jobs (tenant_id, campaign_id, run_id, lead_id, lead_record_id, stage, job_key,
                                 input_result_id, input_result_version, outreach_item_id, config_version_id,
                                 rerun)
           VALUES (%s, %s, %s, %s, %s, 'draft', %s, %s, %s, %s, %s, 0) RETURNING id""",
        (
            job.tenant_id,
            job.campaign_id,
            job.run_id,
            job.lead_id,
            job.lead_record_id,
            key,
            result_id,
            version,
            created["id"],
            job.config_version_id,
        ),
    )["id"]
    return draft_id, reason
