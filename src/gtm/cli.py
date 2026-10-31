"""Command line. The database URL comes from GTM_DATABASE_URL."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from gtm.contracts import EXPORTED
from gtm.db import connect, dsn_from_env
from gtm.pilot.cli import add_pilot_parser, run_pilot
from gtm.pilot.common import REPO_ROOT
from gtm.pilot.models import EXPORTED as PILOT_EXPORTED

FAKE_ONLY = "only --fake-providers is available until the Venice and Deepline adapters land"


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


PILOT_SCHEMAS = REPO_ROOT / "pilot" / "schemas"


def export_contracts(directory: Path, models: dict[str, Any] | None = None) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in (EXPORTED if models is None else models).items():
        path = directory / f"{name}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gtm")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="fictional end-to-end run on a throwaway database")
    demo.add_argument("--fake-providers", action="store_true")

    imp = sub.add_parser("import", help="add contacts from a CSV to the master list")
    imp.add_argument("csv", type=Path)
    imp.add_argument("--source-label", required=True)

    run = sub.add_parser("run", help="queue research for every lead and process all queued work")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--fake-providers", action="store_true")
    run.add_argument(
        "--rerun", type=int, default=0, help="raise to research already-researched leads again"
    )

    tr = sub.add_parser("trace", help="show one lead's full chain")
    tr.add_argument("--lead", type=UUID, required=True)

    sp = sub.add_parser("suppress", help="add an email to the do-not-contact list")
    sp.add_argument("--email", required=True)

    ct = sub.add_parser(
        "contracts", help="export JSON Schemas (pilot schemas go to pilot/schemas/)"
    )
    ct.add_argument("action", choices=["export"])
    ct.add_argument("directory", type=Path, nargs="?", default=Path("docs/contracts"))

    add_pilot_parser(sub)

    args = parser.parse_args(argv)

    if args.command == "pilot":
        run_pilot(args)
    elif args.command == "demo":
        if not args.fake_providers:
            parser.error(FAKE_ONLY)
        from gtm.demo import run_demo

        run_demo()
    elif args.command == "import":
        from gtm.pipeline.importing import import_contacts

        summary = import_contacts(connect(dsn_from_env()), args.csv, args.source_label)
        _print({"import_id": summary.import_id, "rows": summary.rows, "outcomes": summary.outcomes})
    elif args.command == "run":
        if not args.fake_providers:
            parser.error(FAKE_ONLY)
        from gtm.config import load_config_file, resolve
        from gtm.providers.base import Providers
        from gtm.providers.fake import FakeModel, FakeResearchTools
        from gtm.runs import lead_outcomes, start_run
        from gtm.worker import Worker

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
        dsn = dsn_from_env()
        conn = connect(dsn)
        started = start_run(conn, resolve(load_config_file(args.config)), args.rerun)
        worker = Worker(dsn, Providers(FakeModel(), FakeResearchTools()))
        processed = worker.run()
        _print(
            {
                "run": started.__dict__,
                "jobs_processed": len(processed),
                "leads": [
                    {k: r[k] for k in ("id", "state", "state_reason")} for r in lead_outcomes(conn)
                ],
            }
        )
    elif args.command == "trace":
        from gtm.trace import lead_trace, render_trace

        print(render_trace(lead_trace(connect(dsn_from_env()), args.lead)))
    elif args.command == "suppress":
        from gtm.runs import suppress_email

        suppress_email(connect(dsn_from_env()), args.email)
        _print({"suppressed": True})
    elif args.command == "contracts":
        for path in [
            *export_contracts(args.directory),
            *export_contracts(PILOT_SCHEMAS, PILOT_EXPORTED),
        ]:
            print(path)


if __name__ == "__main__":
    main()
