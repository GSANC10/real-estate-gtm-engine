"""The worker: claim one job, run it with fresh context, and commit the result atomically.

Ordinary code decides what runs next. One process handles one lead-stage job at a time. Logs
carry only IDs, stages, and error codes, never contact content.
"""

from __future__ import annotations

import logging
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import psycopg

from gtm.calls import (
    BudgetExceeded,
    CallRecorder,
    InvalidOutput,
    LimitExceeded,
    mark_interrupted_calls,
)
from gtm.context import ContextError, StageContext, StageInputs, build_context
from gtm.db import Conn, connect, maybe_one, tenant_tx
from gtm.faults import NO_FAULTS, CommitResponseLost, Faults
from gtm.pipeline import checks, research, write
from gtm.providers.base import ProviderError, Providers
from gtm.queue import (
    M1_STAGES,
    Completion,
    Job,
    StageOutput,
    StaleLease,
    claim_job,
    complete_stage,
    finish_job,
    heartbeat,
    load_job,
)

log = logging.getLogger("gtm.worker")

PAID_STAGES = {"research", "check", "write"}
RETRY_DELAY_SECONDS = 30


@dataclass(frozen=True)
class Stage:
    build_payload: Callable[[StageInputs], dict[str, Any]]
    execute: Callable[[StageContext, CallRecorder], StageOutput]


STAGES: dict[str, Stage] = {
    "research": Stage(research.build_payload, research.execute),
    "check": Stage(checks.build_payload, checks.execute),
    "write": Stage(write.build_payload, write.execute),
}


@dataclass(frozen=True)
class JobOutcome:
    job_id: UUID
    stage: str
    status: str
    error_code: str | None = None
    completion: Completion | None = None


class Worker:
    def __init__(
        self,
        dsn: str,
        providers: Providers,
        *,
        today: Callable[[], date] = date.today,
        lease_seconds: int = 300,
        stages: tuple[str, ...] = M1_STAGES,
        faults: Faults = NO_FAULTS,
    ) -> None:
        self.dsn = dsn
        self.providers = providers
        self.today = today
        self.lease_seconds = lease_seconds
        self.stages = stages
        self.faults = faults
        self.worker_id = uuid.uuid4()
        self.conn: Conn = connect(dsn)
        self.conn.execute(
            "INSERT INTO gtm.workers (id, hostname) VALUES (%s, %s)",
            (self.worker_id, socket.gethostname()),
        )

    def close(self) -> None:
        self.conn.close()

    def _reconnect(self) -> None:
        try:
            self.conn.close()
        finally:
            self.conn = connect(self.dsn)

    def run(self, max_jobs: int | None = None) -> list[JobOutcome]:
        """Process jobs until none are eligible (or ``max_jobs`` is reached)."""
        outcomes: list[JobOutcome] = []
        while max_jobs is None or len(outcomes) < max_jobs:
            outcome = self.run_once()
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    def run_once(self) -> JobOutcome | None:
        self.conn.execute(
            "UPDATE gtm.workers SET last_seen_at = now() WHERE id = %s", (self.worker_id,)
        )
        claim = claim_job(self.conn, self.worker_id, self.stages, self.lease_seconds)
        if claim is None:
            return None
        log.info("claimed job=%s stage=%s attempt=%s", claim.job_id, claim.stage, claim.attempt)
        try:
            job = load_job(self.conn, claim)
            return self._process(job)
        except StaleLease:
            log.warning("lease lost job=%s", claim.job_id)
            return JobOutcome(claim.job_id, claim.stage, "abandoned", "stale_lease")

    def _process(self, job: Job) -> JobOutcome:
        if job.attempt > job.max_attempts:
            finish_job(self.conn, job, "failed", "attempts_exhausted", lead_state="needs_review")
            return JobOutcome(job.id, job.stage, "failed", "attempts_exhausted")
        if job.attempt > 1:
            mark_interrupted_calls(self.conn, job)

        live = self._live_check(job)
        if live is not None:
            return live
        self.faults.hit("worker.after_claim")

        stage = STAGES[job.stage]
        try:
            ctx = build_context(self.conn, job, self.today(), stage.build_payload)
            calls = CallRecorder(
                conn=self.conn,
                job=job,
                config=ctx.inputs.config,
                limits=getattr(ctx.inputs.config.limits, job.stage),
                providers=self.providers,
                heartbeat=lambda: heartbeat(self.conn, job, self.lease_seconds),
                faults=self.faults,
            )
            output = stage.execute(ctx, calls)
        except ContextError as exc:
            finish_job(self.conn, job, "blocked", exc.code, lead_state="needs_review")
            return JobOutcome(job.id, job.stage, "blocked", exc.code)
        except LimitExceeded as exc:
            finish_job(self.conn, job, "failed", exc.code, lead_state="needs_review")
            return JobOutcome(job.id, job.stage, "failed", exc.code)
        except InvalidOutput as exc:
            finish_job(self.conn, job, "failed", exc.code, lead_state="needs_review")
            return JobOutcome(job.id, job.stage, "failed", exc.code)
        except BudgetExceeded as exc:
            finish_job(self.conn, job, "blocked", exc.code)
            return JobOutcome(job.id, job.stage, "blocked", exc.code)
        except ProviderError as exc:
            # Recorded as uncertain or failed already; retry later under the attempt limit.
            finish_job(self.conn, job, "queued", exc.code, retry_after_seconds=RETRY_DELAY_SECONDS)
            return JobOutcome(job.id, job.stage, "queued", exc.code)

        self.faults.hit("worker.before_complete")
        completion = self._complete(job, ctx.snapshot_id, output)
        log.info("completed job=%s stage=%s replayed=%s", job.id, job.stage, completion.replayed)
        return JobOutcome(job.id, job.stage, "completed", completion=completion)

    def _complete(self, job: Job, snapshot_id: UUID, output: StageOutput) -> Completion:
        """Commit, and if the commit response is lost, resolve it from persisted state."""
        for attempt in range(3):
            try:
                return complete_stage(self.conn, job, snapshot_id, output, self.faults)
            except (CommitResponseLost, psycopg.OperationalError):
                if attempt == 2:
                    raise
                log.warning("completion response lost job=%s; reading persisted state", job.id)
                self._reconnect()
        raise AssertionError("unreachable")

    def _live_check(self, job: Job) -> JobOutcome | None:
        """Re-read live state before paid work: suppression ends the lead, a pause parks the job."""
        if job.stage not in PAID_STAGES:
            return None
        with tenant_tx(self.conn, job.tenant_id):
            row = maybe_one(
                self.conn,
                """SELECT c.status AS campaign_status,
                          EXISTS (SELECT 1 FROM gtm.suppressions s
                                   WHERE s.kind = 'email' AND s.value_hash = l.identity_key) AS suppressed
                     FROM gtm.campaigns c, gtm.leads l
                    WHERE c.id = %s AND l.id = %s""",
                (job.campaign_id, job.lead_id),
            )
        if row is None:
            finish_job(self.conn, job, "blocked", "missing_parent", lead_state="needs_review")
            return JobOutcome(job.id, job.stage, "blocked", "missing_parent")
        if row["suppressed"]:
            finish_job(self.conn, job, "cancelled", "suppressed", lead_state="suppressed")
            return JobOutcome(job.id, job.stage, "cancelled", "suppressed")
        if row["campaign_status"] == "paused":
            finish_job(self.conn, job, "blocked", "campaign_paused")
            return JobOutcome(job.id, job.stage, "blocked", "campaign_paused")
        return None
