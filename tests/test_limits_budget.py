"""Per-job call limits, recorded calls, uncertain outcomes, and the run budget."""

from __future__ import annotations

from decimal import Decimal

from gtm.providers.base import ModelRequest, ModelResponse, ProviderTimeout, ToolCall, Usage
from tests.conftest import JORDAN, Env


def _loop_on_tools(request: ModelRequest) -> ModelResponse:
    return ModelResponse(
        None, [ToolCall("loop", "exa_search", {"query": "Jordan Lee"})], Usage(100, 10), "loop"
    )


def test_tool_loop_stops_at_the_model_call_limit(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, applied, _run = jordan_run
    env.model.script("researcher", *[_loop_on_tools] * 20)
    w = env.worker()
    outcome = w.run(max_jobs=1)[0]
    w.close()
    assert (outcome.status, outcome.error_code) == ("failed", "model_calls_exceeded")
    assert env.lead_state(applied.tenant_id, JORDAN["email"]) == (
        "needs_review",
        "model_calls_exceeded",
    )
    calls = env.rows(
        "SELECT kind, operation, state, input_sha256 FROM gtm.provider_calls ORDER BY seq"
    )
    assert sum(c["kind"] == "model" for c in calls) == 8
    # Searches beyond the limit are refused in code and never reach the provider.
    assert sum(c["operation"] == "exa_search" for c in calls) == 3
    assert all(c["state"] == "succeeded" and c["input_sha256"] for c in calls)


def test_output_token_overrun_stops_the_job(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, _applied, _run = jordan_run
    env.model.script("researcher", ModelResponse("{}", [], Usage(100, 5000), "big"))
    w = env.worker()
    outcome = w.run(max_jobs=1)[0]
    w.close()
    assert outcome.error_code == "output_tokens_exceeded"


def test_invalid_json_gets_one_repair_then_stops(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, applied, _run = jordan_run
    env.model.script(
        "researcher",
        ModelResponse("not json", [], Usage(100, 5), "bad1"),
        ModelResponse('{"citations": []}', [], Usage(100, 5), "bad2"),
    )
    env.tools.pages = ()
    w = env.worker()
    outcome = w.run(max_jobs=1)[0]
    w.close()
    assert outcome.error_code == "invalid_output"
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls WHERE kind = 'model'") == 2
    assert env.lead_state(applied.tenant_id, JORDAN["email"])[0] == "needs_review"


def test_model_timeout_is_uncertain_spend_and_the_job_retries(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, _applied, run = jordan_run
    env.model.script("researcher", ProviderTimeout())
    w = env.worker()
    outcome = w.run(max_jobs=1)[0]
    assert (outcome.status, outcome.error_code) == ("queued", "provider_timeout")
    assert env.scalar("SELECT state FROM gtm.provider_calls") == "uncertain"
    budget = env.rows(
        "SELECT reserved_usd, uncertain_usd FROM gtm.runs WHERE id = %s", (run.run_id,)
    )[0]
    assert budget["reserved_usd"] == 0 and budget["uncertain_usd"] > 0
    env.admin.execute("UPDATE gtm.jobs SET available_at = now()")
    w.run()
    w.close()
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE status = 'completed'") == 3


def test_tiny_budget_pauses_the_run_before_exceeding_the_cap(env: Env) -> None:
    applied = env.brokerage(overrides={"campaign": {"budget_cap_usd": "0.001"}})
    run = env.enqueue(applied, env.import_rows(applied, [JORDAN]))
    w = env.worker()
    outcomes = w.run()
    w.close()
    assert outcomes[-1].status == "blocked" and outcomes[-1].error_code == "budget_exceeded"
    r = env.rows("SELECT * FROM gtm.runs WHERE id = %s", (run.run_id,))[0]
    assert r["status"] == "paused_budget"
    assert r["settled_usd"] + r["reserved_usd"] + r["uncertain_usd"] <= r["budget_cap_usd"]


def test_uncertain_spend_counts_against_the_budget(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, _applied, run = jordan_run
    env.model.script("researcher", ProviderTimeout())
    w = env.worker()
    first = w.run(max_jobs=1)[0]
    assert first.error_code == "provider_timeout"
    reservation = env.scalar("SELECT cost_reserved_usd FROM gtm.provider_calls")
    # Room for one more identical reservation only if the timed-out call were treated as free.
    env.admin.execute(
        "UPDATE gtm.runs SET budget_cap_usd = %s WHERE id = %s",
        (reservation * Decimal("1.5"), run.run_id),
    )
    env.admin.execute("UPDATE gtm.jobs SET available_at = now()")
    second = w.run(max_jobs=1)[0]
    w.close()
    assert (second.status, second.error_code) == ("blocked", "budget_exceeded")
    assert (
        env.scalar("SELECT count(*) FROM gtm.provider_calls") == 1
    )  # nothing dispatched after the timeout
    assert env.scalar("SELECT uncertain_usd FROM gtm.runs") == reservation
