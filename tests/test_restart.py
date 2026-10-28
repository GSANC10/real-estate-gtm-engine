"""Restarts reuse completed work; a stale lease can never commit."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from gtm.context import build_context
from gtm.faults import CommitResponseLost, Faults, SimulatedCrash
from gtm.queue import (
    ConflictingCompletion,
    StageOutput,
    StaleLease,
    claim_job,
    complete_stage,
    heartbeat,
    load_job,
)
from gtm.worker import STAGES, Worker
from tests.conftest import TODAY, Env

STAGE_ORDER = ["research", "check", "write"]


def _calls_per_job(env: Env) -> dict[Any, int]:
    rows = env.rows("SELECT job_id, count(*) AS n FROM gtm.provider_calls GROUP BY job_id")
    return {r["job_id"]: r["n"] for r in rows}


def _calls_after_completion(env: Env) -> int:
    n: int = env.scalar(
        """SELECT count(*) FROM gtm.provider_calls p JOIN gtm.jobs j ON j.id = p.job_id
            WHERE j.completed_at IS NOT NULL AND p.created_at > j.completed_at"""
    )
    return n


@pytest.mark.parametrize("crash_after", STAGE_ORDER)
def test_crash_after_each_stage_commit_resumes_downstream(
    jordan_run: tuple, crash_after: str
) -> None:  # type: ignore[type-arg]
    env, _applied, _run = jordan_run
    faults = Faults()
    faults.arm("complete.after_commit", skip=STAGE_ORDER.index(crash_after))
    first = env.worker(faults)
    with pytest.raises(SimulatedCrash):
        first.run()
    first.close()
    done = env.rows("SELECT stage FROM gtm.jobs WHERE status = 'completed' ORDER BY completed_at")
    assert [d["stage"] for d in done] == STAGE_ORDER[: STAGE_ORDER.index(crash_after) + 1]
    calls_before = _calls_per_job(env)

    second = env.worker()
    second.run()
    second.close()
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE status = 'completed'") == 3
    # Completed jobs were not repeated: their provider call counts are unchanged.
    after = _calls_per_job(env)
    assert all(after[job] == n for job, n in calls_before.items())
    assert _calls_after_completion(env) == 0
    assert env.scalar("SELECT count(*) FROM gtm.stage_results") == 3


def test_crash_during_provider_call_is_recovered_after_lease_expiry(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, _applied, run = jordan_run
    faults = Faults()
    faults.arm("provider.model")
    first = env.worker(faults)
    with pytest.raises(SimulatedCrash):
        first.run()
    first.close()
    assert env.scalar("SELECT state FROM gtm.provider_calls") == "reserved"
    # Still leased: a new worker cannot take it until the lease expires.
    second = env.worker()
    assert second.run() == []
    env.expire_leases()
    second.run()
    second.close()
    assert env.scalar("SELECT attempts FROM gtm.jobs WHERE stage = 'research'") == 2
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls WHERE state = 'uncertain'") == 1
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE status = 'completed'") == 3
    budget = env.rows(
        "SELECT reserved_usd, uncertain_usd FROM gtm.runs WHERE id = %s", (run.run_id,)
    )[0]
    assert budget["reserved_usd"] == 0 and budget["uncertain_usd"] > Decimal(0)


def test_failure_before_commit_leaves_no_partial_result_or_orphan_job(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, _applied, _run = jordan_run
    faults = Faults()
    faults.arm("complete.before_commit")
    first = env.worker(faults)
    with pytest.raises(SimulatedCrash):
        first.run()
    first.close()
    assert env.scalar("SELECT count(*) FROM gtm.stage_results") == 0
    assert env.scalar("SELECT count(*) FROM gtm.citations") == 0
    assert env.scalar("SELECT count(*) FROM gtm.jobs") == 1
    env.expire_leases()
    second = env.worker()
    second.run()
    second.close()
    assert env.scalar("SELECT count(*) FROM gtm.stage_results WHERE kind = 'research'") == 1
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE stage = 'check'") == 1


def test_lost_commit_response_is_resolved_from_persisted_state(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, _applied, _run = jordan_run
    faults = Faults()
    faults.arm("complete.after_commit", error=CommitResponseLost)
    w = env.worker(faults)
    first = w.run(max_jobs=1)[0]
    assert first.completion is not None and first.completion.replayed is True
    model_calls = env.scalar("SELECT count(*) FROM gtm.provider_calls WHERE kind = 'model'")
    w.run()
    w.close()
    assert env.scalar("SELECT count(*) FROM gtm.stage_results WHERE kind = 'research'") == 1
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE stage = 'check'") == 1
    assert model_calls == 3  # inference was not repeated to recover the lost response


def test_stale_lease_cannot_commit_and_conflicting_replay_is_rejected(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, _applied, _run = jordan_run
    old = Worker(env.dsns.worker, env.providers, today=lambda: TODAY)
    claim = claim_job(old.conn, old.worker_id, ("research",), 60)
    assert claim is not None and claim.fencing_token == 1
    job = load_job(old.conn, claim)
    stage = STAGES["research"]
    from gtm.calls import CallRecorder

    ctx = build_context(old.conn, job, TODAY, stage.build_payload)
    calls = CallRecorder(
        old.conn,
        job,
        ctx.inputs.config,
        ctx.inputs.config.limits.research,
        env.providers,
        heartbeat=lambda: None,
    )
    output = stage.execute(ctx, calls)

    env.expire_leases()
    with pytest.raises(StaleLease):
        heartbeat(old.conn, job, 60)
    new = env.worker()
    taken = claim_job(new.conn, new.worker_id, ("research",), 60)
    assert taken is not None and taken.job_id == job.id and taken.fencing_token == 2
    with pytest.raises(StaleLease):
        complete_stage(old.conn, job, ctx.snapshot_id, output)
    assert env.scalar("SELECT count(*) FROM gtm.stage_results") == 0

    new_job = load_job(new.conn, taken)
    first = complete_stage(new.conn, new_job, ctx.snapshot_id, output)
    again = complete_stage(new.conn, new_job, ctx.snapshot_id, output)
    assert (
        again.replayed
        and again.result_id == first.result_id
        and again.next_job_id == first.next_job_id
    )
    different = StageOutput(**{**output.__dict__, "content": {**output.content, "rounds": 99}})
    with pytest.raises(ConflictingCompletion):
        complete_stage(new.conn, new_job, ctx.snapshot_id, different)
    assert env.scalar("SELECT count(*) FROM gtm.stage_results") == 1
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE stage = 'check'") == 1
    old.close()
    new.close()


def test_retry_reuses_the_saved_context_snapshot(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, _applied, _run = jordan_run
    faults = Faults()
    faults.arm("worker.before_complete")
    first = env.worker(faults)
    with pytest.raises(SimulatedCrash):
        first.run()
    first.close()
    snapshot = env.rows("SELECT id, payload_sha256 FROM gtm.context_snapshots")
    env.expire_leases()
    second = env.worker()
    second.run(max_jobs=1)
    second.close()
    research = env.rows("SELECT context_snapshot_id FROM gtm.stage_results WHERE kind = 'research'")
    assert len(snapshot) == 1 and research[0]["context_snapshot_id"] == snapshot[0]["id"]
