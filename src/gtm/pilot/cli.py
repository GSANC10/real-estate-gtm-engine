"""``gtm pilot ...``: discover (a dry run unless --execute), verify, check-scored, report, status."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

from gtm.pilot.common import (
    DEFAULT_CRITERIA,
    DEFAULT_DATA_DIR,
    load_criteria,
    read_costs,
    read_json,
)
from gtm.pilot.models import Criteria
from gtm.util import sha256_hex


def add_pilot_parser(sub: Any) -> None:
    pilot = sub.add_parser(
        "pilot", help="opportunity-report pilot: discover, verify, score, report"
    )
    ps = pilot.add_subparsers(dest="pilot_command", required=True)

    d = ps.add_parser(
        "discover", help="find candidates with Venice web search; a dry run unless --execute"
    )
    d.add_argument("--criteria", type=Path, default=DEFAULT_CRITERIA)
    d.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    d.add_argument(
        "--label", default="discovery", help="short name for the run folder (a-z, 0-9, -)"
    )
    d.add_argument(
        "--only", action="append", metavar="SEARCH_ID", help="run only this search (repeatable)"
    )
    d.add_argument(
        "--resume",
        type=Path,
        metavar="RUN_DIR",
        help="continue a run; searches with a saved response are reused, not paid for again",
    )
    d.add_argument(
        "--max-usd",
        type=float,
        default=0.50,
        help="cap for the whole run, earlier spending included",
    )
    d.add_argument("--execute", action="store_true", help="send the requests (billable)")
    d.add_argument(
        "--show-prompt", action="store_true", help="print the full system prompt in the plan"
    )

    for name, text in (
        ("verify", "fetch every source and check quotes, dates, areas, duplicates (free)"),
        ("check-scored", "validate scored.json against the rubric rules"),
        ("report", "build report.csv and report.html from scored.json"),
        ("status", "show which stages a run has finished and the next command"),
    ):
        p = ps.add_parser(name, help=text)
        p.add_argument("--run-dir", type=Path, required=True)


def run_pilot(args: argparse.Namespace) -> None:
    if args.pilot_command == "discover":
        _discover(args)
    elif args.pilot_command == "verify":
        from gtm.pilot.verify import verify_run

        criteria = Criteria.model_validate(read_json(args.run_dir / "run.json")["criteria"])
        results = verify_run(args.run_dir, criteria, date.today())
        counts: dict[str, int] = {}
        for v in results:
            counts[v.status] = counts.get(v.status, 0) + 1
        print(f"Wrote {args.run_dir / 'verified.json'}: {counts or 'no candidates'}")
        for v in results:
            note = ", ".join(v.reasons + v.warnings) or "ok"
            print(f"  {v.opportunity_id}  {v.status:<10} {note}")
        print(f"Next, in Claude Code from the repo folder: /score-leads {args.run_dir}")
    elif args.pilot_command == "check-scored":
        from gtm.pilot.report import check_scored

        problems = check_scored(args.run_dir)
        for p in problems:
            print(f"PROBLEM {p}")
        print("scored.json: " + ("fix the problems above" if problems else "valid"))
        sys.exit(1 if problems else 0)
    elif args.pilot_command == "report":
        from gtm.pilot.report import build_report

        meta = build_report(args.run_dir)
        print(f"Wrote {args.run_dir / 'report.html'} and {args.run_dir / 'report.csv'}")
        print(
            f"  {meta['shown']} opportunities shown, {meta['check_by_hand']} to check by hand; "
            f"outcomes {meta['counts']}; cost ${meta['costs']['total_usd']:.4f}"
        )
    elif args.pilot_command == "status":
        print(run_status(args.run_dir))


def run_status(run_dir: Path) -> str:
    def has(name: str) -> bool:
        return (run_dir / name).exists()

    spent = sum(float(c.get("usd") or 0) for c in read_costs(run_dir))
    lines = [f"Run {run_dir.name}: ${spent:.4f} spent on {len(read_costs(run_dir))} paid requests"]
    pending = (
        read_json(run_dir / "discovery-summary.json")["searches_pending"]
        if has("discovery-summary.json")
        else []
    )
    for name in ("discovered.json", "verified.json", "scored.json", "report.html"):
        lines.append(f"  {'done' if has(name) else '----'}  {name}")
    if pending:
        lines.append(f"  searches not yet run: {', '.join(pending)}")
    if not has("discovered.json") or (pending and not has("verified.json")):
        nxt = f"uv run gtm pilot discover --resume {run_dir}  (add --execute to send)"
    elif not has("verified.json"):
        nxt = f"uv run gtm pilot verify --run-dir {run_dir}"
    elif not has("scored.json"):
        nxt = f"/score-leads {run_dir}  (in Claude Code, from the repo folder)"
    elif not has("report.html"):
        nxt = f"uv run gtm pilot report --run-dir {run_dir}"
    else:
        nxt = f"review {run_dir / 'report.html'}; the agent's verdicts go in report.csv"
    lines.append(f"Next: {nxt}")
    return "\n".join(lines)


def _discover(args: argparse.Namespace) -> None:
    from gtm.pilot.discover import PROMPT_FILE, run_discovery, system_prompt

    criteria_text = args.criteria.read_text()
    result = run_discovery(
        load_criteria(args.criteria),
        criteria_text,
        args.data_dir,
        args.label,
        execute=args.execute,
        max_usd=args.max_usd,
        today=date.today(),
        only=args.only,
        resume=args.resume,
    )
    if not args.execute:
        criteria = (
            Criteria.model_validate(read_json(args.resume / "run.json")["criteria"])
            if args.resume
            else load_criteria(args.criteria)
        )
        prompt = system_prompt(criteria)
        to_send = [p for p in result.planned if not p.done]
        worst = sum(p.worst_case_usd for p in to_send)
        print(
            f"Plan: {len(to_send)} Venice requests to {criteria.model}, web search on, citations on, "
            f"Venice's own system prompt off, reasoning {criteria.reasoning_effort}, "
            f"max {criteria.max_output_tokens} output tokens each. "
            f"Up to {criteria.max_per_search} candidates per search, {criteria.max_candidates} in all."
        )
        source = f"the criteria saved in {args.resume}" if args.resume else str(args.criteria)
        print(
            f"System prompt: {PROMPT_FILE.relative_to(PROMPT_FILE.parents[2])} filled from {source} "
            f"(sha256 {sha256_hex(prompt)[:12]})"
        )
        if args.show_prompt:
            print("-" * 60 + "\n" + prompt + "\n" + "-" * 60)
        for n, p in enumerate(result.planned, start=1):
            state = (
                "saved response, reused at no cost"
                if p.done
                else f"worst case ${p.worst_case_usd:.4f}"
            )
            print(f"{n}. {p.task.id} ({p.task.signal}), {state}")
            for line in p.payload["messages"][1]["content"].splitlines():
                print(f"     | {line}")
        print(
            f"Worst case for {len(to_send)} new requests: ${worst:.4f} "
            f"(assumes {30_000:,} tokens of search results each; the run's cap is ${args.max_usd:.2f})."
        )
        where = args.resume or f"{args.data_dir}/<UTC date>/<UTC time>-{args.label}/"
        print(f"Output folder: {where}")
        print("Nothing was sent. Add --execute to run it.")
        return
    print(f"Run folder: {result.run_dir}")
    print(
        f"  {result.requests_sent} requests sent, {result.reused} saved responses reused, "
        f"{result.candidates} candidates, ${result.spent_usd:.4f} spent on this run so far"
        + (f", stopped: {result.stopped_reason}" if result.stopped_reason else "")
    )
    for problem in result.problems:
        print(f"  problem: {problem}")
    if result.pending:
        print(f"  searches not yet run: {', '.join(result.pending)}")
        print(
            f"Next: uv run gtm pilot discover --resume {result.run_dir} --execute   (or verify what's here)"
        )
    if result.stopped_reason != "no_web_sources":
        print(f"Then: uv run gtm pilot verify --run-dir {result.run_dir}")
