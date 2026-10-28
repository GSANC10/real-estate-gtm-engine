"""Input-based job keys, pinned versions, and the separate outreach identity."""

from __future__ import annotations

import json

from tests.conftest import JORDAN, Env


def test_retrigger_with_unchanged_inputs_reuses_completed_jobs(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, applied, _run = jordan_run
    w = env.worker()
    w.run()
    calls = env.scalar("SELECT count(*) FROM gtm.provider_calls")
    imported_again = env.import_rows(applied, [JORDAN])
    again = env.enqueue(applied, imported_again, "t2")
    assert (again.jobs_created, again.jobs_reused) == (0, 1)
    assert w.run() == []
    w.close()
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls") == calls
    assert env.scalar("SELECT count(*) FROM gtm.lead_records") == 1
    assert env.scalar("SELECT count(*) FROM gtm.run_leads") == 2


def test_explicit_rerun_creates_new_jobs(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, applied, run = jordan_run
    w = env.worker()
    w.run()
    imported = env.rows("SELECT import_id FROM gtm.runs WHERE id = %s", (run.run_id,))[0][
        "import_id"
    ]
    from gtm.operator import enqueue_run

    rerun = enqueue_run(
        env.trigger, applied.tenant_id, applied.campaign_id, imported, "rerun-1", rerun=1
    )
    assert rerun.jobs_created == 1
    w.run()
    w.close()
    assert env.scalar("SELECT count(*) FROM gtm.stage_results WHERE kind = 'research'") == 2


def test_revised_contact_gets_new_versions_but_no_second_first_touch(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, applied, _run = jordan_run
    w = env.worker()
    w.run()
    revised = {
        **JORDAN,
        "need_note": "I am looking for a four-bedroom home in Torrance. Can someone help?",
    }
    imported = env.import_rows(applied, [revised])
    again = env.enqueue(applied, imported, "t2")
    assert again.jobs_created == 1
    w.run()
    w.close()
    versions = env.rows(
        "SELECT kind, max(version) AS v FROM gtm.stage_results GROUP BY kind ORDER BY kind"
    )
    assert {r["kind"]: r["v"] for r in versions} == {
        "checked_research": 2,
        "research": 2,
        "writing": 2,
    }
    assert env.scalar("SELECT max(version) FROM gtm.lead_records") == 2
    assert env.scalar("SELECT count(*) FROM gtm.outreach_items") == 1
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE stage = 'draft'") == 1
    assert env.lead_state(applied.tenant_id, JORDAN["email"]) == (
        "ready_for_draft",
        "outreach_item_exists",
    )


def test_queued_jobs_keep_their_pinned_configuration(jordan_run: tuple) -> None:  # type: ignore[type-arg]
    env, _applied, _run = jordan_run
    w = env.worker()
    w.run(max_jobs=1)  # research done; check job queued under config v1
    newer = env.brokerage(
        overrides={"campaign": {"tone_reference": "A newer tone reference written after queueing."}}
    )
    assert newer.version == 2
    w.run()
    w.close()
    assert env.scalar("SELECT count(DISTINCT config_version_id) FROM gtm.jobs") == 1
    snap = env.rows(
        """SELECT s.payload FROM gtm.context_snapshots s JOIN gtm.jobs j ON j.id = s.job_id WHERE j.stage = 'write'"""
    )[0]
    writer_input = json.loads(snap["payload"]["messages"][1]["content"])
    assert "newer tone" not in writer_input["campaign"]["tone_reference"]


def test_stage_fingerprints_cover_only_relevant_settings(env: Env) -> None:
    first = env.brokerage()
    changed = env.brokerage(overrides={"campaign": {"tone_reference": "Different style sample."}})
    fps = {
        r["version"]: r["content"]["fingerprints"]
        for r in env.rows(
            "SELECT version, content FROM gtm.config_versions WHERE campaign_id = %s",
            (first.campaign_id,),
        )
    }
    assert changed.version == 2
    assert fps[1]["research"] == fps[2]["research"] and fps[1]["check"] == fps[2]["check"]
    assert fps[1]["write"] != fps[2]["write"]
    same = env.brokerage(overrides={"campaign": {"tone_reference": "Different style sample."}})
    assert same.created is False and same.version == 2
