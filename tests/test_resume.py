"""Stopping the program never loses or repeats finished work."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

import gtm.queue
import gtm.worker
from gtm.calls import CallRecorder
from gtm.context import build_context
from gtm.providers.base import ModelRequest, ModelResponse
from gtm.queue import ConflictingCompletion, StageOutput, complete_stage, next_job
from gtm.worker import STAGE_IMPL
from tests.conftest import TODAY, Env, Interrupted


def _calls_per_job(env: Env) -> dict[Any, int]:
    return {
        r["job_id"]: r["n"]
        for r in env.rows("SELECT job_id, count(*) AS n FROM gtm.provider_calls GROUP BY job_id")
    }


@pytest.mark.parametrize("finished", [1, 2, 3])
def test_restart_after_each_finished_stage_reuses_it(jordan_run: Env, finished: int) -> None:
    env = jordan_run
    env.work(max_jobs=finished)
    before = _calls_per_job(env)
    env.work()
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE status = 'completed'") == 3
    after = _calls_per_job(env)
    assert all(after[job] == n for job, n in before.items())
    assert (
        env.scalar(
            """SELECT count(*) FROM gtm.provider_calls p JOIN gtm.jobs j ON j.id = p.job_id
            WHERE p.created_at > j.completed_at"""
        )
        == 0
    )


def test_job_interrupted_mid_call_is_requeued_and_its_call_counted(jordan_run: Env) -> None:
    env = jordan_run

    def killed(_request: ModelRequest) -> ModelResponse:
        raise Interrupted()

    env.model.script("researcher", killed)
    with pytest.raises(Interrupted):
        env.work()
    assert env.scalar("SELECT status FROM gtm.jobs") == "running"
    assert env.scalar("SELECT state FROM gtm.provider_calls") == "started"

    env.work()  # a new program re-queues the interrupted job at startup
    assert env.scalar("SELECT attempts FROM gtm.jobs WHERE stage = 'research'") == 2
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls WHERE state = 'uncertain'") == 1
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE status = 'completed'") == 3
    estimate = env.scalar(
        "SELECT cost_estimate_usd FROM gtm.provider_calls WHERE state = 'uncertain'"
    )
    spent = env.scalar("SELECT spent_usd FROM gtm.runs")
    settled = env.scalar("SELECT sum(cost_usd) FROM gtm.provider_calls WHERE state <> 'uncertain'")
    assert spent == settled + estimate


def test_failure_inside_completion_leaves_nothing_half_saved(
    jordan_run: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = jordan_run

    def crash(*_args: Any) -> None:
        raise Interrupted()

    monkeypatch.setattr(gtm.queue, "set_lead_state", crash)
    with pytest.raises(Interrupted):
        env.work(max_jobs=1)
    assert env.scalar("SELECT count(*) FROM gtm.stage_results") == 0
    assert env.scalar("SELECT count(*) FROM gtm.jobs") == 1
    monkeypatch.undo()
    env.work()
    assert env.scalar("SELECT count(*) FROM gtm.stage_results WHERE kind = 'research'") == 1
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE stage = 'check'") == 1


def test_lost_commit_response_is_replayed_without_new_calls(
    jordan_run: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = jordan_run
    real = gtm.worker.complete_stage
    lost = {"once": True}

    def commit_then_drop(*args: Any) -> Any:
        result = real(*args)
        if lost.pop("once", False):
            raise psycopg.OperationalError("connection dropped after commit")
        return result

    monkeypatch.setattr(gtm.worker, "complete_stage", commit_then_drop)
    first = env.work(max_jobs=1)[0]
    assert first.completion is not None and first.completion.replayed is True
    assert env.scalar("SELECT count(*) FROM gtm.stage_results") == 1
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE stage = 'check'") == 1
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls WHERE kind = 'model'") == 3


def test_repeated_completion_is_idempotent_and_conflicts_are_rejected(jordan_run: Env) -> None:
    env = jordan_run
    job = next_job(env.conn)
    assert job is not None
    stage = STAGE_IMPL["research"]
    ctx = build_context(env.conn, job, TODAY, stage.build_payload)
    output = stage.execute(
        ctx,
        CallRecorder(
            env.conn, job, ctx.inputs.config, ctx.inputs.config.limits.research, env.providers
        ),
    )
    first = complete_stage(env.conn, job, output)
    again = complete_stage(env.conn, job, output)
    assert again.replayed and (again.result_id, again.next_job_id) == (
        first.result_id,
        first.next_job_id,
    )
    different = StageOutput(**{**output.__dict__, "content": {**output.content, "rounds": 99}})
    with pytest.raises(ConflictingCompletion):
        complete_stage(env.conn, job, different)
    assert env.scalar("SELECT count(*) FROM gtm.stage_results") == 1
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE stage = 'check'") == 1
