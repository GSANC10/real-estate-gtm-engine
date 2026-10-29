"""The fictional handoff: import -> research -> check -> writer context from the checked result -> writing."""

from __future__ import annotations

import json
from typing import Any

from gtm.runs import suppress_email
from tests.conftest import JORDAN, Env


def _jobs(env: Env) -> dict[str, dict[str, Any]]:
    return {
        j["stage"]: j
        for j in env.rows(
            "SELECT * FROM gtm.jobs WHERE lead_id = %s", (env.lead_id(JORDAN["email"]),)
        )
    }


def _writer_input(env: Env) -> dict[str, Any]:
    call = env.rows(
        "SELECT input FROM gtm.provider_calls WHERE operation = 'writer' ORDER BY seq LIMIT 1"
    )[0]
    data: dict[str, Any] = json.loads(call["input"]["messages"][1]["content"])
    return data


def test_single_lead_handoff_chain(jordan_run: Env) -> None:
    env = jordan_run
    outcomes = env.work()
    assert [(o.stage, o.status) for o in outcomes] == [
        ("research", "completed"),
        ("check", "completed"),
        ("write", "completed"),
    ]
    jobs = _jobs(env)
    results = {r["kind"]: r for r in env.rows("SELECT * FROM gtm.stage_results")}

    # Each job consumes the exact result its parent produced.
    assert jobs["check"]["input_result_id"] == results["research"]["id"]
    assert jobs["write"]["input_result_id"] == results["checked_research"]["id"]
    assert results["checked_research"]["parent_result_id"] == results["research"]["id"]
    assert results["checked_research"]["outcome"] == "qualified"
    assert len({j["run_id"] for j in jobs.values()}) == 1

    # The writer's input was built from the checked result and nothing else.
    writer_input = _writer_input(env)
    checked = results["checked_research"]["content"]
    assert [f["id"] for f in writer_input["findings"]] == [f["id"] for f in checked["findings"]]
    raw = json.dumps(writer_input)
    for forbidden in (
        JORDAN["email"],
        "Riverfront",
        "Ignore previous instructions",
        "soccer",
        "Fictional sender profile note",
        "sender_fact_3",
    ):
        assert forbidden not in raw, forbidden

    writing = results["writing"]["content"]
    assert results["writing"]["outcome"] == "passed"
    known = {f["id"] for f in writer_input["findings"]} | {
        f["id"] for f in writer_input["sender"]["facts"]
    }
    assert all(ref in known for c in writing["output"]["claims"] for ref in c["refs"])
    assert writing["email_check"]["status"] == "completed"

    outreach = env.rows("SELECT * FROM gtm.outreach_items")
    assert len(outreach) == 1 and outreach[0]["writing_result_id"] == results["writing"]["id"]
    assert (
        jobs["draft"]["status"] == "queued"
        and jobs["draft"]["job_key"] == f"draft/{outreach[0]['id']}"
    )
    assert env.lead_state(JORDAN["email"]) == ("ready_for_draft", None)


def test_research_never_sees_the_email_and_wrong_person_is_excluded(jordan_run: Env) -> None:
    env = jordan_run
    env.work()
    research_inputs = env.rows("SELECT input FROM gtm.provider_calls WHERE stage = 'research'")
    assert all(JORDAN["email"] not in json.dumps(r["input"]) for r in research_inputs)

    checked = env.rows("SELECT content FROM gtm.stage_results WHERE kind = 'checked_research'")[0][
        "content"
    ]
    by_key = {c["citation_key"]: c for c in checked["citation_checks"]}
    assert (
        by_key["C1"]["passed"] and by_key["C2"]["passed"]
    )  # supplied record; page naming the organization
    assert (
        not by_key["C3"]["passed"] and by_key["C3"]["identity"] == "fail"
    )  # a different Jordan Lee

    calls = env.rows(
        "SELECT kind, operation, state, cost_usd FROM gtm.provider_calls WHERE stage = 'research'"
    )
    assert [c["operation"] for c in calls].count("researcher") == 3
    assert {c["operation"] for c in calls if c["kind"] == "tool"} == {"exa_search", "read_page"}
    assert all(c["state"] == "succeeded" and c["cost_usd"] > 0 for c in calls)


def test_every_contact_reaches_an_honest_outcome(env: Env) -> None:
    suppress_email(env.conn, "sam.okafor@mail.example")
    imported = env.import_example()
    assert imported.outcomes == {"imported": 4, "suppressed": 1}
    env.start()
    env.work()
    # Lives in Seattle, asks for a home in Torrance: service fit uses the property location.
    assert env.lead_state("jordan.lee@mail.example") == ("ready_for_draft", None)
    assert env.lead_state("avery.park@mail.example") == (
        "disqualified",
        "property_location_outside_service_area",
    )
    assert env.lead_state("morgan.diaz@mail.example") == ("insufficient_evidence", "evidence_stale")
    assert env.lead_state("riley.chen@mail.example") == (
        "insufficient_evidence",
        "property_location_missing",
    )
    assert (
        env.scalar("SELECT count(*) FROM gtm.leads") == 4
    )  # the suppressed contact is never stored
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE stage = 'write'") == 1
    assert env.scalar("SELECT count(*) FROM gtm.outreach_items") == 1


def test_new_suppression_stops_spending(jordan_run: Env) -> None:
    env = jordan_run
    env.work(max_jobs=1)
    calls = env.scalar("SELECT count(*) FROM gtm.provider_calls")
    suppress_email(env.conn, JORDAN["email"])
    outcome = env.work()[0]
    assert (outcome.stage, outcome.status, outcome.error_code) == (
        "check",
        "cancelled",
        "suppressed",
    )
    assert env.lead_state(JORDAN["email"]) == ("suppressed", "suppressed")
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls") == calls


def test_oversized_context_blocks_with_a_reason(env: Env) -> None:
    env.import_rows([JORDAN])
    env.start({"limits": {"research": {"input_tokens": 500}}})
    outcome = env.work()[0]
    assert (outcome.status, outcome.error_code) == ("blocked", "context_too_large")
    assert env.lead_state(JORDAN["email"]) == ("needs_review", "context_too_large")
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls") == 0
