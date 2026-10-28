"""The fictional handoff: import -> research -> check -> writer context from the checked result -> writing."""

from __future__ import annotations

import json
from typing import Any

from gtm.operator import suppress_email
from tests.conftest import JORDAN, Env


def _jobs(env: Env, lead_id: Any) -> dict[str, dict[str, Any]]:
    return {
        j["stage"]: j for j in env.rows("SELECT * FROM gtm.jobs WHERE lead_id = %s", (lead_id,))
    }


def test_single_lead_handoff_chain(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, applied, _run = jordan_run
    w = env.worker()
    outcomes = w.run()
    w.close()
    assert [(o.stage, o.status) for o in outcomes] == [
        ("research", "completed"),
        ("check", "completed"),
        ("write", "completed"),
    ]
    lead = env.lead_id(applied.tenant_id, JORDAN["email"])
    jobs = _jobs(env, lead)
    results = {
        r["kind"]: r
        for r in env.rows("SELECT * FROM gtm.stage_results WHERE lead_id = %s", (lead,))
    }

    # Each job pins its exact parent result and version, and inherits the parent's config version.
    assert jobs["check"]["input_result_id"] == results["research"]["id"]
    assert jobs["write"]["input_result_id"] == results["checked_research"]["id"]
    assert jobs["write"]["input_result_version"] == results["checked_research"]["version"] == 1
    assert len({j["config_version_id"] for j in jobs.values()}) == 1
    assert results["checked_research"]["parent_result_id"] == results["research"]["id"]
    assert results["checked_research"]["outcome"] == "qualified"

    # The exact writer input is a private snapshot built from the checked result.
    snap = env.rows(
        "SELECT * FROM gtm.context_snapshots WHERE job_id = %s", (jobs["write"]["id"],)
    )[0]
    assert results["writing"]["context_snapshot_id"] == snap["id"]
    writer_input = json.loads(snap["payload"]["messages"][1]["content"])
    checked = results["checked_research"]["content"]
    assert [f["id"] for f in writer_input["findings"]] == [f["id"] for f in checked["findings"]]
    raw = json.dumps(snap["payload"])
    for forbidden in (
        "jordan.lee@mail.example",
        "Riverfront",
        "Ignore previous instructions",
        "soccer",
        "Fictional sender profile note",
        "sender_fact_3",
    ):
        assert forbidden not in raw, forbidden

    # Writing passed both checks and every claim resolves to a supplied finding or sender fact.
    writing = results["writing"]["content"]
    assert results["writing"]["outcome"] == "passed"
    known = {f["id"] for f in writer_input["findings"]} | {
        f["id"] for f in writer_input["sender"]["facts"]
    }
    assert all(ref in known for c in writing["output"]["claims"] for ref in c["refs"])
    assert writing["email_check"]["status"] == "completed"

    # One first-touch outreach item and a queued draft job keyed by it.
    outreach = env.rows("SELECT * FROM gtm.outreach_items WHERE lead_id = %s", (lead,))
    assert len(outreach) == 1 and outreach[0]["writing_result_id"] == results["writing"]["id"]
    assert (
        jobs["draft"]["status"] == "queued"
        and jobs["draft"]["job_key"] == f"draft/{outreach[0]['id']}"
    )
    assert env.lead_state(applied.tenant_id, JORDAN["email"]) == ("ready_for_draft", None)


def test_research_context_excludes_email_and_untrusted_page_is_excluded(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, applied, _run = jordan_run
    w = env.worker()
    w.run()
    w.close()
    lead = env.lead_id(applied.tenant_id, JORDAN["email"])
    jobs = _jobs(env, lead)
    research_snap = env.rows(
        "SELECT payload FROM gtm.context_snapshots WHERE job_id = %s", (jobs["research"]["id"],)
    )
    assert JORDAN["email"] not in json.dumps(research_snap[0]["payload"])

    checks = env.rows(
        """SELECT c.url, k.identity, k.passed, k.reasons FROM gtm.citation_checks k
             JOIN gtm.citations c ON c.id = k.citation_id ORDER BY c.key"""
    )
    by_url = {c["url"]: c for c in checks}
    assert by_url[None]["passed"] is True
    assert by_url["https://news.example/2026/09/community-design-day"]["passed"] is True
    portland = by_url["https://runners.example/results/riverfront-10k"]
    assert portland["passed"] is False and portland["identity"] == "fail"

    # Every model call and tool round was recorded before dispatch with its input and cost.
    calls = env.rows(
        "SELECT kind, operation, state, input, cost_settled_usd FROM gtm.provider_calls WHERE job_id = %s",
        (jobs["research"]["id"],),
    )
    assert [c["operation"] for c in calls].count("researcher") == 3
    assert {c["operation"] for c in calls if c["kind"] == "tool"} == {"exa_search", "read_page"}
    assert all(
        c["state"] == "succeeded" and c["input"] and c["cost_settled_usd"] > 0 for c in calls
    )


def test_every_contact_reaches_an_honest_outcome(env: Env) -> None:
    applied = env.brokerage()
    suppress_email(env.app, applied.tenant_id, "sam.okafor@mail.example")
    imported = env.import_example(applied)
    assert imported.outcomes == {"imported": 4, "suppressed": 1}
    env.enqueue(applied, imported)
    w = env.worker()
    w.run()
    w.close()
    tenant = applied.tenant_id
    # Lives in Seattle, asks for a home in Torrance: service fit uses the property location.
    assert env.lead_state(tenant, "jordan.lee@mail.example") == ("ready_for_draft", None)
    assert env.lead_state(tenant, "avery.park@mail.example") == (
        "disqualified",
        "property_location_outside_service_area",
    )
    assert env.lead_state(tenant, "morgan.diaz@mail.example") == (
        "insufficient_evidence",
        "evidence_stale",
    )
    assert env.lead_state(tenant, "riley.chen@mail.example") == (
        "insufficient_evidence",
        "property_location_missing",
    )
    assert (
        env.scalar("SELECT count(*) FROM gtm.leads WHERE tenant_id = %s", (tenant,)) == 4
    )  # Sam never stored
    # Drafts exist only for qualified contacts.
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE stage = 'write'") == 1
    assert env.scalar("SELECT count(*) FROM gtm.outreach_items") == 1
