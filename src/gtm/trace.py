"""``gtm trace``: the full chain for one lead.

record -> research (citations) -> checks and decision -> exact writer input -> writing and
email checks -> outreach item -> draft job, with the provider calls and cost of every job.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from gtm.db import Conn, all_rows, maybe_one


def lead_trace(conn: Conn, lead_id: UUID) -> dict[str, Any]:
    jobs = all_rows(
        conn, "SELECT * FROM gtm.jobs WHERE lead_id = %s ORDER BY created_at, id", (lead_id,)
    )
    return {
        "lead": maybe_one(
            conn, "SELECT id, state, state_reason FROM gtm.leads WHERE id = %s", (lead_id,)
        ),
        "records": all_rows(
            conn,
            "SELECT id, version, content FROM gtm.lead_records WHERE lead_id = %s ORDER BY version",
            (lead_id,),
        ),
        "jobs": jobs,
        "results": all_rows(
            conn,
            "SELECT * FROM gtm.stage_results WHERE lead_id = %s ORDER BY created_at",
            (lead_id,),
        ),
        "calls": all_rows(
            conn,
            "SELECT * FROM gtm.provider_calls WHERE job_id = ANY(%s) ORDER BY created_at, seq",
            ([j["id"] for j in jobs],),
        ),
        "outreach": maybe_one(
            conn, "SELECT * FROM gtm.outreach_items WHERE lead_id = %s", (lead_id,)
        ),
    }


def _short(value: Any) -> str:
    return str(value)[:8] if value else "-"


def render_trace(t: dict[str, Any]) -> str:
    lead = t["lead"]
    reason = f" ({lead['state_reason']})" if lead["state_reason"] else ""
    out = [f"Lead {lead['id']} - state: {lead['state']}{reason}"]
    for r in t["records"]:
        c = r["content"]
        need = c["recorded_need"]
        out.append(
            f"  record v{r['version']} {_short(r['id'])}: {c['full_name']}, lives in {c['residence_location']}; "
            f"{need['category']} in {need['property_location'] or '(not given)'}; "
            f"recorded {need['recorded_at']} via {need['source']}"
        )
    for job in t["jobs"]:
        out.append("")
        err = f" error={job['error_code']}" if job["error_code"] else ""
        out.append(
            f"[{job['stage']}] job {_short(job['id'])} {job['status']} (attempts {job['attempts']}){err}"
        )
        out.append(f"  key: {job['job_key']}")
        calls = [c for c in t["calls"] if c["job_id"] == job["id"]]
        if calls:
            cost = sum(c["cost_usd"] or 0 for c in calls)
            model = next((c["model_id"] for c in calls if c["model_id"]), None)
            out.append(
                f"  provider calls ({len(calls)}, ${cost:.6f}, model {model}): "
                + ", ".join(f"{c['operation']}:{c['state']}" for c in calls)
            )
        writer = next((c for c in calls if c["operation"] == "writer"), None)
        if writer:
            out.append("  exact writer input (saved with the call):")
            user = next(m for m in writer["input"]["messages"] if m["role"] == "user")
            out.extend(
                "    " + line
                for line in json.dumps(json.loads(user["content"]), indent=2).splitlines()
            )
        for res in (r for r in t["results"] if r["job_id"] == job["id"]):
            out.extend(_render_result(res))
        if job["stage"] == "draft":
            out.append("  draft operation: not built yet; job stays queued")
    if t["outreach"]:
        o = t["outreach"]
        out += [
            "",
            f"outreach item {_short(o['id'])} ({o['step']}) <- writing {_short(o['writing_result_id'])}",
        ]
    return "\n".join(out)


def _render_result(res: dict[str, Any]) -> list[str]:
    c = res["content"]
    out = [
        f"  -> {res['kind']} v{res['version']} {_short(res['id'])}"
        + (f" outcome={res['outcome']}" if res["outcome"] else "")
    ]
    if res["kind"] == "research":
        for cit in c["citations"]:
            where = cit["url"] or "supplied record"
            out.append(
                f"     {cit['key']} {cit['source']} {where} ({cit['date_basis']} {cit['source_date']}): "
                f'"{cit["quote"]}"'
            )
        for i, f in enumerate(c["output"]["findings"], start=1):
            out.append(f"     F{i} [{f['use']}] {f['text']} <- {', '.join(f['citations'])}")
        out.append(f"     unknowns: {', '.join(c['output']['unknowns']) or 'none'}")
    elif res["kind"] == "checked_research":
        for chk in c["citation_checks"]:
            mark = "pass" if chk["passed"] else "FAIL " + ",".join(chk["reasons"])
            out.append(
                f"     {chk['finding_key']}/{chk['citation_key']}: identity={chk['identity']} "
                f"recency={chk['recency']} meaning={chk['meaning']} -> {mark}"
            )
        why = f" ({', '.join(c['reasons'])})" if c["reasons"] else ""
        fit = c["proposed_fit"]
        out.append(
            f"     decision (code): {c['qualification']}{why}; service_area={c['service_area']} "
            f"property_need={c['property_need']}; researcher proposed {fit['service_area']}/{fit['property_need']}"
        )
        out.extend(f"     kept {f['id']} [{f['use']}]: {f['text']}" for f in c["findings"])
        out.extend(
            f"     excluded [{f['use']}]: {f['text']} ({', '.join(f['reasons'])})"
            for f in c["excluded_findings"]
        )
    elif res["kind"] == "writing":
        o = c["output"]
        out.append(f"     subject: {o['subject']}")
        out.extend("     | " + line for line in o["body"].splitlines())
        out.append(
            f"     code checks: {'passed' if c['code_checks']['passed'] else 'FAILED'}"
            + "".join(
                f'\n       - {f["rule"]}: {f["detail"]} in "{f["sentence"]}"'
                for f in c["code_checks"]["failures"]
            )
        )
        out.append(f"     email check: {c['email_check']['status']}")
        out.extend(f"       {v['verdict']:<12} {v['text']}" for v in c["email_check"]["sentences"])
        out.extend(
            f"     claim -> {', '.join(cl['refs'])}: {cl['sentence']}"
            for cl in o["claims"]
            if cl["refs"]
        )
    return out
