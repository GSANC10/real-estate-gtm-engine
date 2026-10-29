"""Finished work is reused, revisions add versions, and each lead gets at most one first email."""

from __future__ import annotations

import json

from tests.conftest import JORDAN, Env


def test_rerun_with_nothing_changed_reuses_finished_work(jordan_run: Env) -> None:
    env = jordan_run
    env.work()
    calls = env.scalar("SELECT count(*) FROM gtm.provider_calls")
    assert env.import_rows([JORDAN]).outcomes == {"unchanged": 1}
    again = env.start()
    assert (again.jobs_created, again.jobs_reused) == (0, 1)
    assert env.work() == []
    assert env.scalar("SELECT count(*) FROM gtm.provider_calls") == calls


def test_explicit_rerun_researches_again(jordan_run: Env) -> None:
    env = jordan_run
    env.work()
    assert env.start(rerun=1).jobs_created == 1
    env.work()
    assert env.scalar("SELECT count(*) FROM gtm.stage_results WHERE kind = 'research'") == 2


def test_revised_contact_gets_new_versions_but_no_second_first_email(jordan_run: Env) -> None:
    env = jordan_run
    env.work()
    revised = {
        **JORDAN,
        "need_note": "I am looking for a four-bedroom home in Torrance. Can someone help?",
    }
    assert env.import_rows([revised]).outcomes == {"updated": 1}
    assert env.start().jobs_created == 1
    env.work()
    versions = {
        r["kind"]: r["v"]
        for r in env.rows("SELECT kind, max(version) AS v FROM gtm.stage_results GROUP BY kind")
    }
    assert versions == {"checked_research": 2, "research": 2, "writing": 2}
    assert env.scalar("SELECT max(version) FROM gtm.lead_records") == 2
    assert env.scalar("SELECT count(*) FROM gtm.outreach_items") == 1
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE stage = 'draft'") == 1
    assert env.lead_state(JORDAN["email"]) == ("ready_for_draft", "outreach_item_exists")


def test_queued_jobs_keep_their_runs_config(jordan_run: Env) -> None:
    env = jordan_run
    env.work(max_jobs=1)  # research done; its check job belongs to the first run
    env.start({"campaign": {"tone_reference": "A newer tone reference written after queueing."}})
    env.work()
    call = env.rows("SELECT input FROM gtm.provider_calls WHERE operation = 'writer'")[0]
    writer_input = json.loads(call["input"]["messages"][1]["content"])
    assert "newer tone" not in writer_input["campaign"]["tone_reference"]
    assert env.scalar("SELECT count(DISTINCT run_id) FROM gtm.jobs") == 1
