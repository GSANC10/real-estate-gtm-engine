"""``gtm demo --fake-providers``: the fictional end-to-end flow on a throwaway database.

Import -> research -> check -> writer context from the saved checked result -> writing output,
with a worker restart after the first research commit and a re-trigger of the same import.
Every person, brokerage, and web page in this demo is fictional.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from uuid import UUID

from gtm.config import apply_config, load_config_file
from gtm.db import Conn, all_rows, connect, one, tenant_tx
from gtm.devdb import throwaway_database
from gtm.operator import enqueue_run, run_status, suppress_email
from gtm.pipeline.importing import import_contacts
from gtm.providers.base import Providers
from gtm.providers.fake import FakeModel, FakeResearchTools
from gtm.trace import lead_trace, render_trace
from gtm.util import email_identity_key
from gtm.worker import Worker

EXAMPLES = Path(__file__).resolve().parents[2] / "config" / "examples"
# Fixed so the fictional evidence ages are the same whenever the demo runs.
DEMO_TODAY = date(2026, 10, 27)
FOCUS_EMAIL = "jordan.lee@mail.example"
SUPPRESSED_EMAIL = "sam.okafor@mail.example"


def _call_count(conn: Conn, tenant_id: UUID) -> int:
    with tenant_tx(conn, tenant_id):
        n: int = one(conn, "SELECT count(*) AS n FROM gtm.provider_calls")["n"]
    return n


def _role(conn: Conn) -> str:
    row = one(
        conn,
        "SELECT current_user AS u, (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS b",
    )
    return f"{row['u']} (bypass RLS: {row['b']})"


def run_demo(echo: Callable[[str], None] = print) -> None:
    def section(title: str) -> None:
        echo("")
        echo(f"=== {title} ===")

    with throwaway_database() as dsns:
        app = connect(dsns.app)
        trigger = connect(dsns.trigger)
        providers = Providers(FakeModel(), FakeResearchTools())

        section("Setup (throwaway PostgreSQL 17, migrations applied by a non-superuser role)")
        echo(f"operator connection: {_role(app)}")
        echo(f"trigger connection:  {_role(trigger)}")
        applied = apply_config(app, load_config_file(EXAMPLES / "example-homes.yaml"))
        tenant = applied.tenant_id
        echo(
            f"config: Example Homes (fictional) config v{applied.version} {applied.config_version_id}"
        )
        suppress_email(app, tenant, SUPPRESSED_EMAIL)
        echo("suppression: one fictional contact added before import")

        section("1. Import")
        imported = import_contacts(
            app,
            tenant,
            applied.campaign_id,
            EXAMPLES / "contacts-example-homes.csv",
            "fictional demo list",
        )
        echo(f"import {imported.import_id}: {imported.rows} rows -> {imported.outcomes}")
        run = enqueue_run(
            trigger, tenant, applied.campaign_id, imported.import_id, "demo-trigger-1"
        )
        echo(
            f"run {run.run_id}: {run.jobs_created} research jobs created, {run.jobs_reused} reused"
        )

        section("2. Research, then a worker restart")
        first = Worker(dsns.worker, providers, today=lambda: DEMO_TODAY)
        echo(f"worker connection:   {_role(first.conn)}")
        done = first.run(max_jobs=1)
        echo(f"worker A completed {done[0].stage} job {done[0].job_id} and stopped")
        calls_before = _call_count(app, tenant)
        first.close()
        second = Worker(dsns.worker, providers, today=lambda: DEMO_TODAY)
        outcomes = second.run()
        echo(
            f"worker B processed {len(outcomes)} jobs: "
            + ", ".join(f"{o.stage}:{o.status}" for o in outcomes)
        )
        with tenant_tx(app, tenant):
            repeated = one(
                app,
                "SELECT count(*) AS n FROM gtm.provider_calls WHERE job_id = %s AND created_at > ("
                "SELECT completed_at FROM gtm.jobs WHERE id = %s)",
                (done[0].job_id, done[0].job_id),
            )["n"]
        echo(
            f"provider calls for worker A's completed job after restart: {repeated} new "
            f"(total before restart {calls_before})"
        )
        second.close()

        section("3-5. Outcomes for every contact")
        with tenant_tx(app, tenant):
            rows = all_rows(
                app,
                """SELECT r.content->>'full_name' AS name, cl.state, cl.state_reason
                     FROM gtm.campaign_leads cl
                     JOIN gtm.lead_records r ON r.lead_id = cl.lead_id
                    ORDER BY r.created_at""",
            )
        for r in rows:
            echo(f"  {r['name']:<14} {r['state']:<22} {r['state_reason'] or ''}")
        echo(f"  {'Sam Okafor':<14} {'suppressed at import':<22} (no record stored)")
        status = run_status(trigger, tenant, run.run_id)
        echo(
            f"run status: jobs {status['jobs']}; settled ${status['settled_usd']:.6f}, "
            f"reserved ${status['reserved_usd']:.6f}, uncertain ${status['uncertain_usd']:.6f}"
        )

        section("Re-trigger the same import (new trigger id, unchanged inputs)")
        calls_before = _call_count(app, tenant)
        again = enqueue_run(
            trigger, tenant, applied.campaign_id, imported.import_id, "demo-trigger-2"
        )
        third = Worker(dsns.worker, providers, today=lambda: DEMO_TODAY)
        rerun_outcomes = third.run()
        third.close()
        echo(
            f"run {again.run_id}: {again.jobs_created} jobs created, {again.jobs_reused} reused; "
            f"worker processed {len(rerun_outcomes)} jobs; new provider calls: "
            f"{_call_count(app, tenant) - calls_before}"
        )

        section("Handoff trace for the fictional lead Jordan")
        with tenant_tx(app, tenant):
            lead_id = one(
                app,
                "SELECT id FROM gtm.leads WHERE identity_key = %s",
                (email_identity_key(FOCUS_EMAIL),),
            )["id"]
        echo(render_trace(lead_trace(app, tenant, lead_id)))
        app.close()
        trigger.close()
