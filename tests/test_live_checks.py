"""Live state is re-read before paid stages; oversized context blocks instead of truncating."""

from __future__ import annotations

from gtm.operator import set_campaign_status, suppress_email
from tests.conftest import JORDAN, Env


def test_new_suppression_stops_spending(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, applied, _run = jordan_run
    w = env.worker()
    w.run(max_jobs=1)
    calls = env.scalar("SELECT count(*) FROM gtm.provider_calls")
    suppress_email(env.app, applied.tenant_id, JORDAN["email"])
    outcome = w.run()[0]
    w.close()
    assert (outcome.stage, outcome.status, outcome.error_code) == (
        "check",
        "cancelled",
        "suppressed",
    )
    assert env.lead_state(applied.tenant_id, JORDAN["email"]) == ("suppressed", "suppressed")
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls") == calls
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE stage = 'write'") == 0


def test_paused_campaign_blocks_then_resumes(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, applied, _run = jordan_run
    set_campaign_status(env.app, applied.tenant_id, applied.campaign_id, "paused")
    w = env.worker()
    outcome = w.run()[0]
    assert (outcome.status, outcome.error_code) == ("blocked", "campaign_paused")
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls") == 0
    assert set_campaign_status(env.app, applied.tenant_id, applied.campaign_id, "active") == 1
    w.run()
    w.close()
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE status = 'completed'") == 3


def test_oversized_context_blocks_with_a_reason(env: Env) -> None:
    applied = env.brokerage(overrides={"limits": {"research": {"input_tokens": 500}}})
    env.enqueue(applied, env.import_rows(applied, [JORDAN]))
    w = env.worker()
    outcome = w.run()[0]
    w.close()
    assert (outcome.status, outcome.error_code) == ("blocked", "context_too_large")
    assert env.lead_state(applied.tenant_id, JORDAN["email"]) == (
        "needs_review",
        "context_too_large",
    )
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls") == 0
