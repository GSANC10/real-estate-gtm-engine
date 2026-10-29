"""Per-job call limits, recorded calls, timeouts, and the run's spending cap."""

from __future__ import annotations

from decimal import Decimal

from gtm.providers.base import ModelRequest, ModelResponse, ProviderTimeout, ToolCall, Usage
from tests.conftest import JORDAN, Env


def _loop_on_tools(_request: ModelRequest) -> ModelResponse:
    return ModelResponse(
        None, [ToolCall("loop", "exa_search", {"query": "Jordan Lee"})], Usage(100, 10), "loop"
    )


def test_tool_loop_stops_at_the_model_call_limit(jordan_run: Env) -> None:
    env = jordan_run
    env.model.script("researcher", *[_loop_on_tools] * 20)
    outcome = env.work(max_jobs=1)[0]
    assert (outcome.status, outcome.error_code) == ("failed", "model_calls_exceeded")
    assert env.lead_state(JORDAN["email"]) == ("needs_review", "model_calls_exceeded")
    calls = env.rows("SELECT kind, operation, state FROM gtm.provider_calls ORDER BY seq")
    assert sum(c["kind"] == "model" for c in calls) == 8
    # Searches past the limit are refused in code and never reach the provider.
    assert sum(c["operation"] == "exa_search" for c in calls) == 3
    assert all(c["state"] == "succeeded" for c in calls)


def test_output_token_overrun_stops_the_job(jordan_run: Env) -> None:
    env = jordan_run
    env.model.script("researcher", ModelResponse("{}", [], Usage(100, 5000), "big"))
    assert env.work(max_jobs=1)[0].error_code == "output_tokens_exceeded"


def test_invalid_json_gets_one_repair_then_stops(jordan_run: Env) -> None:
    env = jordan_run
    env.model.script(
        "researcher",
        ModelResponse("not json", [], Usage(100, 5), "bad1"),
        ModelResponse('{"citations": []}', [], Usage(100, 5), "bad2"),
    )
    env.tools.pages = ()
    assert env.work(max_jobs=1)[0].error_code == "invalid_output"
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls WHERE kind = 'model'") == 2
    assert env.lead_state(JORDAN["email"])[0] == "needs_review"


def test_timeout_counts_its_estimate_as_spent_and_the_job_retries(jordan_run: Env) -> None:
    env = jordan_run
    env.model.script("researcher", ProviderTimeout())
    outcome = env.work(max_jobs=1)[0]
    assert (outcome.status, outcome.error_code) == ("queued", "provider_timeout")
    call = env.rows("SELECT state, cost_usd, cost_estimate_usd FROM gtm.provider_calls")[0]
    assert call["state"] == "uncertain" and call["cost_usd"] == call["cost_estimate_usd"]
    assert env.scalar("SELECT spent_usd FROM gtm.runs") == call["cost_estimate_usd"]
    env.conn.execute("UPDATE gtm.jobs SET available_at = now()")
    env.work()
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE status = 'completed'") == 3


def test_tiny_budget_pauses_the_run_before_exceeding_the_cap(env: Env) -> None:
    env.import_rows([JORDAN])
    env.start({"campaign": {"budget_cap_usd": "0.001"}})
    outcome = env.work()[-1]
    assert (outcome.status, outcome.error_code) == ("blocked", "budget_exceeded")
    run = env.rows("SELECT * FROM gtm.runs")[0]
    assert run["status"] == "paused_budget" and run["spent_usd"] <= run["budget_cap_usd"]


def test_timed_out_spend_blocks_the_next_call_when_the_cap_is_tight(jordan_run: Env) -> None:
    env = jordan_run
    env.model.script("researcher", ProviderTimeout())
    env.work(max_jobs=1)
    estimate = env.scalar("SELECT cost_estimate_usd FROM gtm.provider_calls")
    # Room for one more call only if the timed-out one were treated as free.
    env.conn.execute("UPDATE gtm.runs SET budget_cap_usd = %s", (estimate * Decimal("1.5"),))
    env.conn.execute("UPDATE gtm.jobs SET available_at = now()")
    outcome = env.work(max_jobs=1)[0]
    assert (outcome.status, outcome.error_code) == ("blocked", "budget_exceeded")
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls") == 1
