"""``gtm demo --fake-providers``: the fictional end-to-end flow on a throwaway database.

Import -> research -> check -> writer context from the saved checked result -> writing output,
with the program stopped after the first finished job and restarted, and a second run that
reuses finished work. Every person, brokerage, and web page in this demo is fictional.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

from gtm.config import load_config_file, resolve
from gtm.db import connect, one
from gtm.devdb import throwaway_database
from gtm.pipeline.importing import import_contacts
from gtm.providers.base import Providers
from gtm.providers.fake import FakeModel, FakeResearchTools
from gtm.runs import lead_outcomes, start_run, suppress_email
from gtm.trace import lead_trace, render_trace
from gtm.util import email_identity_key
from gtm.worker import Worker

EXAMPLES = Path(__file__).resolve().parents[2] / "config" / "examples"
# Fixed so the fictional evidence ages are the same whenever the demo runs.
DEMO_TODAY = date(2026, 10, 27)
FOCUS_EMAIL = "jordan.lee@mail.example"
SUPPRESSED_EMAIL = "sam.okafor@mail.example"


def run_demo(echo: Callable[[str], None] = print) -> None:
    def section(title: str) -> None:
        echo("")
        echo(f"=== {title} ===")

    with throwaway_database() as dsn:
        conn = connect(dsn)
        providers = Providers(FakeModel(), FakeResearchTools())
        config = resolve(load_config_file(EXAMPLES / "example-homes.yaml"))

        section("1. Import (fictional brokerage: Example Homes)")
        suppress_email(conn, SUPPRESSED_EMAIL)
        imported = import_contacts(
            conn, EXAMPLES / "contacts-example-homes.csv", "fictional demo list"
        )
        echo(
            f"{imported.rows} rows -> {imported.outcomes} (one contact was on the do-not-contact list)"
        )
        run = start_run(conn, config)
        echo(f"run {run.run_id}: {run.jobs_created} research jobs queued")

        section("2. Stop after the first finished job, then restart")
        first = Worker(dsn, providers, today=lambda: DEMO_TODAY)
        done = first.run(max_jobs=1)[0]
        first.close()
        calls_before = one(
            conn, "SELECT count(*) AS n FROM gtm.provider_calls WHERE job_id = %s", (done.job_id,)
        )["n"]
        second = Worker(dsn, providers, today=lambda: DEMO_TODAY)
        outcomes = second.run()
        second.close()
        calls_after = one(
            conn, "SELECT count(*) AS n FROM gtm.provider_calls WHERE job_id = %s", (done.job_id,)
        )["n"]
        echo(
            f"first program finished 1 {done.stage} job; the restarted program processed {len(outcomes)} more"
        )
        echo(
            f"provider calls for the finished job: {calls_before} before restart, {calls_after} after"
        )

        section("3. Outcomes")
        for row in lead_outcomes(conn):
            echo(f"  {row['name']:<14} {row['state']:<22} {row['state_reason'] or ''}")
        spent = one(
            conn, "SELECT spent_usd, budget_cap_usd FROM gtm.runs WHERE id = %s", (run.run_id,)
        )
        echo(f"spent ${spent['spent_usd']:.6f} of a ${spent['budget_cap_usd']:.2f} cap")

        section("4. Run again with nothing changed")
        calls_before = one(conn, "SELECT count(*) AS n FROM gtm.provider_calls")["n"]
        again = start_run(conn, config)
        rerun = Worker(dsn, providers, today=lambda: DEMO_TODAY)
        processed = rerun.run()
        rerun.close()
        new_calls = one(conn, "SELECT count(*) AS n FROM gtm.provider_calls")["n"] - calls_before
        echo(
            f"{again.jobs_created} jobs created, {again.jobs_reused} reused, {len(processed)} processed, "
            f"{new_calls} new provider calls"
        )

        section("5. Handoff trace for the fictional lead Jordan")
        lead_id = one(
            conn,
            "SELECT id FROM gtm.leads WHERE identity_key = %s",
            (email_identity_key(FOCUS_EMAIL),),
        )["id"]
        echo(render_trace(lead_trace(conn, lead_id)))
        conn.close()
