"""Operator CLI. Each command connects as the least-privileged role it needs.

Database URLs come from GTM_APP_DATABASE_URL, GTM_TRIGGER_DATABASE_URL, and
GTM_WORKER_DATABASE_URL. Output carries IDs and counts; ``trace`` is a private operator view.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from gtm.config import apply_config, campaign_id_for_slug, load_config_file
from gtm.contracts import EXPORTED
from gtm.db import connect, dsn_from_env

FAKE_ONLY = (
    "only --fake-providers is available until the hosted model, Deepline, and Gmail adapters land"
)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def export_contracts(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in EXPORTED.items():
        path = directory / f"{name}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gtm")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="fictional end-to-end run on a throwaway database")
    demo.add_argument("--fake-providers", action="store_true")

    cfg = sub.add_parser("config", help="validate and store brokerage configuration")
    cfg_sub = cfg.add_subparsers(dest="action", required=True)
    cfg_apply = cfg_sub.add_parser("apply")
    cfg_apply.add_argument("path", type=Path)

    imp = sub.add_parser("import", help="import a private contact CSV")
    imp.add_argument("csv", type=Path)
    imp.add_argument("--tenant-id", type=UUID, required=True)
    imp.add_argument("--campaign", required=True, help="campaign slug")
    imp.add_argument("--source-label", required=True)

    enq = sub.add_parser("enqueue", help="start a run for an import (trigger role)")
    enq.add_argument("--tenant-id", type=UUID, required=True)
    enq.add_argument("--campaign-id", type=UUID, required=True)
    enq.add_argument("--import-id", type=UUID, required=True)
    enq.add_argument("--trigger-id", required=True)

    st = sub.add_parser("status", help="run progress: IDs and counts only (trigger role)")
    st.add_argument("--tenant-id", type=UUID, required=True)
    st.add_argument("--run-id", type=UUID, required=True)

    wk = sub.add_parser("worker", help="process queued jobs")
    wk.add_argument("--fake-providers", action="store_true")
    wk.add_argument("--max-jobs", type=int)

    tr = sub.add_parser("trace", help="show one lead's full handoff chain (private)")
    tr.add_argument("--tenant-id", type=UUID, required=True)
    tr.add_argument("--lead-id", type=UUID, required=True)

    sp = sub.add_parser("suppress", help="suppress an email address for a tenant")
    sp.add_argument("--tenant-id", type=UUID, required=True)
    sp.add_argument("--email", required=True)

    cp = sub.add_parser("campaign", help="pause or resume a campaign")
    cp.add_argument("action", choices=["pause", "resume"])
    cp.add_argument("--tenant-id", type=UUID, required=True)
    cp.add_argument("--campaign", required=True, help="campaign slug")

    ct = sub.add_parser("contracts", help="export JSON Schemas for the contracts")
    ct.add_argument("action", choices=["export"])
    ct.add_argument("directory", type=Path, nargs="?", default=Path("docs/contracts"))

    args = parser.parse_args(argv)

    if args.command == "demo":
        if not args.fake_providers:
            parser.error(FAKE_ONLY)
        from gtm.demo import run_demo

        run_demo()
    elif args.command == "config":
        applied = apply_config(connect(dsn_from_env("app")), load_config_file(args.path))
        _print(applied.__dict__)
    elif args.command == "import":
        from gtm.pipeline.importing import import_contacts

        conn = connect(dsn_from_env("app"))
        campaign_id = campaign_id_for_slug(conn, args.tenant_id, args.campaign)
        summary = import_contacts(conn, args.tenant_id, campaign_id, args.csv, args.source_label)
        _print(
            {
                "import_id": summary.import_id,
                "campaign_id": campaign_id,
                "rows": summary.rows,
                "outcomes": summary.outcomes,
            }
        )
    elif args.command == "enqueue":
        from gtm.operator import enqueue_run

        result = enqueue_run(
            connect(dsn_from_env("trigger")),
            args.tenant_id,
            args.campaign_id,
            args.import_id,
            args.trigger_id,
        )
        _print(result.__dict__)
    elif args.command == "status":
        from gtm.operator import run_status

        _print(run_status(connect(dsn_from_env("trigger")), args.tenant_id, args.run_id))
    elif args.command == "worker":
        if not args.fake_providers:
            parser.error(FAKE_ONLY)
        from gtm.providers.base import Providers
        from gtm.providers.fake import FakeModel, FakeResearchTools
        from gtm.worker import Worker

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
        worker = Worker(dsn_from_env("worker"), Providers(FakeModel(), FakeResearchTools()))
        outcomes = worker.run(max_jobs=args.max_jobs)
        _print(
            [
                {
                    "job_id": o.job_id,
                    "stage": o.stage,
                    "status": o.status,
                    "error_code": o.error_code,
                }
                for o in outcomes
            ]
        )
    elif args.command == "trace":
        from gtm.trace import lead_trace, render_trace

        print(render_trace(lead_trace(connect(dsn_from_env("app")), args.tenant_id, args.lead_id)))
    elif args.command == "suppress":
        from gtm.operator import suppress_email

        suppress_email(connect(dsn_from_env("app")), args.tenant_id, args.email)
        _print({"suppressed": True})
    elif args.command == "campaign":
        from gtm.operator import set_campaign_status

        conn = connect(dsn_from_env("app"))
        campaign_id = campaign_id_for_slug(conn, args.tenant_id, args.campaign)
        requeued = set_campaign_status(
            conn, args.tenant_id, campaign_id, "paused" if args.action == "pause" else "active"
        )
        _print({"campaign_id": campaign_id, "status": args.action, "jobs_requeued": requeued})
    elif args.command == "contracts":
        for path in export_contracts(args.directory):
            print(path)


if __name__ == "__main__":
    main()
