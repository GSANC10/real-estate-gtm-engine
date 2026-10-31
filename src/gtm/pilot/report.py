"""Check scored.json against the rubric's rules, then build report.csv and report.html.

The report lists verified, scored opportunities best first, with evidence links, the score and
why, confidence and gaps, and a suggested next step. Candidates whose sources couldn't be fetched
are listed separately, unscored, for a person to check by hand. Phone numbers and email addresses
are removed from everything shown; the agent reaches owners through the listing itself. Columns
for the agent's own verdict are left blank so his feedback can be recorded per opportunity.
"""

from __future__ import annotations

import csv
import html
import io
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from gtm.pilot.common import read_costs, read_json, write_new
from gtm.pilot.models import Criteria, Scored, Verified
from gtm.validation import EMAIL_RE, PROTECTED

# North American phone numbers only, so dates (2026-10-30) and ZIP+4 codes are left alone.
PHONE_RE = re.compile(
    r"(?<![\d-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?![\d-])"
)
CONTRADICTION_WARNINGS = {"source_mentions_sold_or_pending", "source_mentions_listing_agent"}
SIGNAL_LABELS = {
    "fsbo_listing": "For sale by owner",
    "public_intent_to_sell": "Public intent to sell",
    "business_relocation_with_property": "Business relocation with property",
    "expired_or_withdrawn_listing": "Expired or withdrawn listing",
    "active_listing_price_reduction": "Price reduction (listed with another agent)",
    "buyer_seeking_agent_or_financing": "Buyer asking for an agent or financing",
}
REPRESENTATION_LABELS = {
    "no_agent_stated": "no agent stated",
    "agent_involved": "agent involved",
    "unknown": "agent status unknown",
}
CSV_FIELDS = [
    "rank",
    "verification",
    "opportunity_id",
    "signal",
    "area",
    "property",
    "representation",
    "summary",
    "score",
    "confidence",
    "recommendation",
    "explanation",
    "facts",
    "inferences",
    "contradictions",
    "evidence_urls",
    "newest_source_date",
    "missing_info",
    "warnings",
    "next_step",
    "agent_verdict",
    "agent_notes",
]


def scrub(text: str) -> str:
    return PHONE_RE.sub("[phone removed]", EMAIL_RE.sub("[email removed]", text))


def load_verified(run_dir: Path) -> list[Verified]:
    return [Verified.model_validate(r) for r in read_json(run_dir / "verified.json")]


def load_scored(run_dir: Path) -> list[Scored]:
    return [Scored.model_validate(r) for r in read_json(run_dir / "scored.json")]


def check_scored(run_dir: Path) -> list[str]:
    """Problems that must be fixed before a report is built. Empty means the file is valid."""
    verified = {v.opportunity_id: v for v in load_verified(run_dir)}
    try:
        scored = load_scored(run_dir)
    except ValidationError as exc:
        return [
            f"scored.json does not match the scored.v1 schema: {e['loc']}: {e['msg']}"
            for e in exc.errors()[:20]
        ]
    problems: list[str] = []
    seen: set[str] = set()
    for s in scored:
        sid = s.opportunity_id
        if sid in seen:
            problems.append(f"{sid}: scored twice")
        seen.add(sid)
        v = verified.get(sid)
        if v is None or v.status != "verified":
            problems.append(f"{sid}: not a verified opportunity in verified.json")
            continue
        d = s.dimensions
        if s.score != d.intent + d.evidence + d.relevance + d.recency:
            problems.append(f"{sid}: score must equal the sum of the four dimensions")
        checked_urls = {c.url for c in v.checks if c.quote_found}
        for fact in s.facts:
            if fact.source_url not in checked_urls:
                problems.append(
                    f"{sid}: fact cites a URL whose quote wasn't verified: {fact.source_url}"
                )
        text = " ".join(
            [
                s.explanation,
                s.next_step,
                *s.inferences,
                *s.contradictions,
                *s.missing_info,
                *(f.statement for f in s.facts),
            ]
        )
        if PROTECTED.search(text):
            problems.append(f"{sid}: mentions a protected personal characteristic")
        if EMAIL_RE.search(text) or PHONE_RE.search(text):
            problems.append(f"{sid}: contains a phone number or email address")
        if s.recommendation == "include" and s.confidence == "low":
            problems.append(f"{sid}: 'include' needs medium or high confidence")
        if v.newest_source_date is None and d.recency > 0:
            problems.append(f"{sid}: recency must be 0 when no source date was confirmed")
        flagged = CONTRADICTION_WARNINGS & set(v.warnings)
        if flagged and s.recommendation == "include" and not s.contradictions:
            problems.append(
                f"{sid}: verification flagged {sorted(flagged)}; read the snapshot and record "
                "what it says in contradictions, or don't include it"
            )
    for sid, v in verified.items():
        if v.status == "verified" and sid not in seen:
            problems.append(
                f"{sid}: verified but not scored (score it, or reject it with a reason)"
            )
    return problems


def cost_summary(run_dir: Path, verified: int, included: int) -> dict[str, Any]:
    entries = read_costs(run_dir)
    total = sum(float(e.get("usd") or 0) for e in entries)

    def per(n: int) -> float | None:
        return round(total / n, 4) if n else None

    return {
        "total_usd": round(total, 4),
        "paid_requests": len(entries),
        "estimated_entries": sum(1 for e in entries if e.get("usd_basis") != "reported_usage"),
        "per_verified_opportunity_usd": per(verified),
        "per_included_opportunity_usd": per(included),
    }


def _context(v: Verified) -> dict[str, Any]:
    c = v.candidate
    p = c.property
    return {
        "opportunity_id": v.opportunity_id,
        "signal": SIGNAL_LABELS.get(c.signal, c.signal),
        "area": ", ".join(x for x in (p.neighborhood, p.city) if x),
        "property": " · ".join(
            x for x in (p.address, p.property_type.replace("_", " "), p.asking_price) if x
        ),
        "representation": REPRESENTATION_LABELS[c.representation],
        "summary": scrub(c.summary),
        "newest_source_date": v.newest_source_date or "not confirmed",
        "warnings": v.warnings,
    }


def _rows(run_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    criteria = Criteria.model_validate(read_json(run_dir / "run.json")["criteria"])
    verified = load_verified(run_dir)
    by_id = {v.opportunity_id: v for v in verified}
    scored = sorted(load_scored(run_dir), key=lambda s: -s.score)
    shown = [s for s in scored if s.recommendation != "reject"][: criteria.report_size]
    rows = []
    for rank, s in enumerate(shown, start=1):
        v = by_id[s.opportunity_id]
        rows.append(
            {
                **_context(v),
                "rank": rank,
                "verification": "verified",
                "score": s.score,
                "confidence": s.confidence,
                "recommendation": s.recommendation.replace("_", " "),
                "explanation": scrub(s.explanation),
                "facts": [(scrub(f.statement), f.source_url) for f in s.facts],
                "inferences": [scrub(i) for i in s.inferences],
                "contradictions": [scrub(c) for c in s.contradictions],
                "missing_info": [scrub(m) for m in s.missing_info],
                "next_step": scrub(s.next_step),
                "evidence_urls": sorted({c.url for c in v.checks if c.quote_found}),
            }
        )
    manual = [
        {
            **_context(v),
            "verification": "unverified",
            "recommendation": "check by hand",
            "explanation": "Not checked automatically: the source site blocked or didn't answer the request.",
            "missing_info": [scrub(m) for m in v.candidate.missing_info],
            "evidence_urls": [
                ev.url
                for ev, d in zip(v.candidate.evidence, v.candidate.discovery_checks, strict=True)
                if d.url_in_search_results
            ],
            "next_step": "Open the link and confirm the quote and date before acting on it.",
        }
        for v in verified
        if v.status == "unverified"
    ]
    counts: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for v in verified:
        counts[v.status] = counts.get(v.status, 0) + 1
        if v.status != "verified":
            for r in v.reasons:
                reasons[r] = reasons.get(r, 0) + 1
    meta = {
        "run_id": run_dir.name,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "counts": counts,
        "not_included_reasons": reasons,
        "scored": len(scored),
        "shown": len(rows),
        "check_by_hand": len(manual),
        "costs": cost_summary(run_dir, counts.get("verified", 0), len(rows)),
    }
    return rows, manual, meta


def to_csv(rows: list[dict[str, Any]], manual: list[dict[str, Any]]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for r in [*rows, *manual]:
        writer.writerow(
            {
                **{k: r[k] for k in CSV_FIELDS if k in r and not isinstance(r[k], list)},
                "facts": " | ".join(f"{s} ({u})" for s, u in r.get("facts", [])),
                "inferences": "; ".join(r.get("inferences", [])),
                "contradictions": "; ".join(r.get("contradictions", [])),
                "evidence_urls": " ".join(r["evidence_urls"]),
                "missing_info": "; ".join(r["missing_info"]),
                "warnings": "; ".join(r["warnings"]),
                "agent_verdict": "",
                "agent_notes": "",
            }
        )
    return out.getvalue()


_CSS = """
:root { --ink:#1d2430; --muted:#5b6575; --line:#dfe3ea; --bg:#f6f7f9; --card:#fff; --accent:#1f5fbf; --warn:#8a5a00; }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color:var(--ink); background:var(--bg); }
main { max-width: 880px; margin: 0 auto; padding: 28px 16px 48px; }
h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 18px; margin: 28px 0 10px; }
.sub { color: var(--muted); margin: 0 0 18px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; margin: 14px 0; }
.top { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; flex-wrap: wrap; }
.score { font-size: 22px; font-weight: 700; color: var(--accent); }
.tag { display: inline-block; font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #eef2f8; color: var(--muted); margin-right: 6px; }
ul { margin: 6px 0 6px 18px; padding: 0; } li { margin: 2px 0; }
a { color: var(--accent); word-break: break-all; }
.label { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin-top: 10px; }
table { border-collapse: collapse; width: 100%; } td { border-top: 1px solid var(--line); padding: 6px 4px; vertical-align: top; }
.note { color: var(--muted); font-size: 13px; } .warn { color: var(--warn); font-size: 13px; }
"""


def _items(values: list[str], empty: str) -> str:
    return "".join(f"<li>{html.escape(v)}</li>" for v in values) or f"<li>{empty}</li>"


def _links(urls: list[str]) -> str:
    e = html.escape
    return "".join(f'<li><a href="{e(u)}">{e(u)}</a></li>' for u in urls)


def to_html(rows: list[dict[str, Any]], manual: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    e = html.escape
    cards = []
    for r in rows:
        facts = "".join(f'<li>{e(s)} <a href="{e(u)}">source</a></li>' for s, u in r["facts"])
        contra = (
            f'<div class="label">Contradictions</div><ul>{_items(r["contradictions"], "")}</ul>'
            if r["contradictions"]
            else ""
        )
        warn = (
            f'<p class="warn">Check by hand: {e(", ".join(r["warnings"]))}</p>'
            if r["warnings"]
            else ""
        )
        cards.append(f"""
<section class="card">
  <div class="top"><div><span class="tag">#{r["rank"]}</span><span class="tag">{e(r["signal"])}</span>
  <span class="tag">{e(r["area"])}</span><span class="tag">{e(r["representation"])}</span></div>
  <div class="score">{r["score"]}/100</div></div>
  <p><strong>{e(r["summary"])}</strong><br><span class="note">{e(r["property"])}</span></p>
  <p>{e(r["explanation"])}</p>
  <div class="label">Verified facts</div><ul>{facts}</ul>
  <div class="label">Inferences (not confirmed)</div><ul>{_items(r["inferences"], "None")}</ul>{contra}
  <div class="label">Missing information</div><ul>{_items(r["missing_info"], "Nothing noted")}</ul>
  <div class="label">Sources (newest dated {e(r["newest_source_date"])})</div><ul>{_links(r["evidence_urls"])}</ul>
  <p><strong>Confidence:</strong> {e(r["confidence"])} · <strong>Suggested next step:</strong> {e(r["next_step"])}</p>{warn}
</section>""")
    by_hand = "".join(
        f"""
<section class="card">
  <div class="top"><div><span class="tag">not verified</span><span class="tag">{e(m["signal"])}</span>
  <span class="tag">{e(m["area"])}</span></div></div>
  <p><strong>{e(m["summary"])}</strong><br><span class="note">{e(m["property"])}</span></p>
  <ul>{_links(m["evidence_urls"])}</ul>
</section>"""
        for m in manual
    )
    by_hand_section = (
        "<h2>Couldn't check automatically</h2><p class=\"note\">These sites blocked or didn't answer our "
        "check, so nothing here is confirmed or scored. Open each link and confirm before acting.</p>"
        + by_hand
        if manual
        else ""
    )
    c = meta["costs"]
    counts = ", ".join(f"{k}: {v}" for k, v in sorted(meta["counts"].items())) or "none"
    reasons = (
        ", ".join(
            f"{k.replace('_', ' ')} ({v})" for k, v in sorted(meta["not_included_reasons"].items())
        )
        or "none"
    )

    def usd(x: float | None) -> str:
        return "n/a" if x is None else f"${x:.4f}"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Real estate opportunities</title><style>{_CSS}</style></head>
<body><main>
<h1>Real estate opportunities</h1>
<p class="sub">Run {e(meta["run_id"])} · built {e(meta["built_at"])} · {meta["shown"]} opportunities shown, best first</p>
<p class="note">Every fact below was found, quoted, on the linked public page when this report was built.
Scores follow a draft rubric (intent 40, evidence 25, relevance 20, recency 15) that we want your feedback on.
Phone numbers and emails are removed; use the contact method on each listing.</p>
{"".join(cards) or "<p>No opportunities passed verification in this run.</p>"}
{by_hand_section}
<h2>How this run went</h2>
<table>
<tr><td>Candidates by outcome</td><td>{e(counts)}</td></tr>
<tr><td>Why candidates were left out</td><td>{e(reasons)}</td></tr>
<tr><td>Research cost</td><td>{usd(c["total_usd"])} total · {usd(c["per_verified_opportunity_usd"])} per verified opportunity</td></tr>
</table>
<p class="note">Your verdict on each opportunity (pursue, maybe, or no) tells us what to look for next.</p>
</main></body></html>
"""


def build_report(run_dir: Path) -> dict[str, Any]:
    problems = check_scored(run_dir)
    if problems:
        raise ValueError(
            "scored.json has problems; run `gtm pilot check-scored` and fix them first"
        )
    rows, manual, meta = _rows(run_dir)
    write_new(run_dir / "report.csv", to_csv(rows, manual))
    write_new(run_dir / "report.html", to_html(rows, manual, meta))
    return meta
