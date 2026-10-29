"""The worker: take the next job, run it with fresh context, and commit the result atomically.

Ordinary code decides what runs next. One program handles one lead-stage job at a time. Logs
carry only IDs, stages, and error codes, never contact content.
"""

from __future__ import annotations

import logging
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
from gtm.db import Conn, connect, one
from gtm.pipeline import checks, research, write
from gtm.providers.base import ProviderError, Providers
from gtm.queue import (
    STAGES,
    Completion,
    Job,
    StageOutput,
    complete_stage,
    finish_job,
    next_job,
    recover_interrupted,
)

log = logging.getLogger("gtm.worker")

RETRY_DELAY_SECONDS = 30


@dataclass(frozen=True)
class Stage:
    build_payload: Callable[[StageInputs], dict[str, Any]]
    execute: Callable[[StageContext, CallRecorder], StageOutput]


STAGE_IMPL: dict[str, Stage] = {
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
        stages: tuple[str, ...] = STAGES,
    ) -> None:
        self.dsn = dsn
        self.providers = providers
        self.today = today
        self.stages = stages
        self.conn: Conn = connect(dsn)
        recovered = recover_interrupted(self.conn)
        if recovered:
            log.info("re-queued %s interrupted jobs", recovered)

    def close(self) -> None:
        self.conn.close()

    def run(self, max_jobs: int | None = None) -> list[JobOutcome]:
        """Process jobs until none are queued (or ``max_jobs`` is reached)."""
        outcomes: list[JobOutcome] = []
        while max_jobs is None or len(outcomes) < max_jobs:
            job = next_job(self.conn, self.stages)
            if job is None:
                break
            log.info("job=%s stage=%s attempt=%s", job.id, job.stage, job.attempt)
            outcomes.append(self._process(job))
        return outcomes

    def _process(self, job: Job) -> JobOutcome:
        if job.attempt > job.max_attempts:
            return self._stop(job, "failed", "attempts_exhausted", lead_state="needs_review")
        if job.attempt > 1:
            mark_interrupted_calls(self.conn, job)
        if self._suppressed(job):
            return self._stop(job, "cancelled", "suppressed", lead_state="suppressed")

        stage = STAGE_IMPL[job.stage]
        try:
            ctx = build_context(self.conn, job, self.today(), stage.build_payload)
            calls = CallRecorder(
                self.conn,
                job,
                ctx.inputs.config,
                getattr(ctx.inputs.config.limits, job.stage),
                self.providers,
            )
            output = stage.execute(ctx, calls)
        except ContextError as exc:
            return self._stop(job, "blocked", exc.code, lead_state="needs_review")
        except (LimitExceeded, InvalidOutput) as exc:
            return self._stop(job, "failed", exc.code, lead_state="needs_review")
        except BudgetExceeded as exc:
            return self._stop(job, "blocked", exc.code)
        except ProviderError as exc:
            # Already recorded as uncertain or failed; try again later, up to max_attempts.
            finish_job(self.conn, job, "queued", exc.code, retry_after_seconds=RETRY_DELAY_SECONDS)
            return JobOutcome(job.id, job.stage, "queued", exc.code)

        completion = self._complete(job, output)
        return JobOutcome(job.id, job.stage, "completed", completion=completion)

    def _stop(self, job: Job, status: str, code: str, lead_state: str | None = None) -> JobOutcome:
        finish_job(self.conn, job, status, code, lead_state=lead_state)
        return JobOutcome(job.id, job.stage, status, code)

    def _complete(self, job: Job, output: StageOutput) -> Completion:
        """Commit. If the connection drops, reconnect and retry: a commit that did land is replayed."""
        for attempt in range(3):
            try:
                return complete_stage(self.conn, job, output)
            except psycopg.OperationalError:
                if attempt == 2:
                    raise
                log.warning("completion response lost job=%s; retrying", job.id)
                self.conn.close()
                self.conn = connect(self.dsn)
        raise AssertionError("unreachable")

    def _suppressed(self, job: Job) -> bool:
        """Re-read the do-not-contact list before any paid stage."""
        row = one(
            self.conn,
            """SELECT EXISTS (SELECT 1 FROM gtm.suppressions s WHERE s.value_hash = l.identity_key) AS hit
                 FROM gtm.leads l WHERE l.id = %s""",
            (job.lead_id,),
        )
        return bool(row["hit"])
