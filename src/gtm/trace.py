"""``gtm trace``: the full chain for one lead, read as the operator role under tenant scope.

imported record -> research result -> citations and checks -> qualification decision
-> writer job and exact writer input -> writing result -> outreach item -> draft job
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from gtm.db import Conn, all_rows, maybe_one, tenant_tx


def lead_trace(conn: Conn, tenant_id: UUID, lead_id: UUID) -> dict[str, Any]:
    with tenant_tx(conn, tenant_id):
        records = all_rows(
            conn,
            "SELECT id, version, content, created_at FROM gtm.lead_records WHERE lead_id = %s ORDER BY version",
            (lead_id,),
        )
        states = all_rows(
            conn,
            "SELECT campaign_id, state, state_reason FROM gtm.campaign_leads WHERE lead_id = %s",
            (lead_id,),
        )
        jobs = all_rows(
            conn,
            """SELECT j.id, j.stage, j.status, j.job_key, j.input_result_id, j.input_result_version, j.outreach_item_id,
                      j.prompt_version, j.prompt_sha256, j.model_id, j.attempts, j.error_code, cv.version AS config_version,
                      j.lead_record_id
                 FROM gtm.jobs j JOIN gtm.config_versions cv ON cv.id = j.config_version_id
                WHERE j.lead_id = %s ORDER BY j.created_at, j.id""",
            (lead_id,),
        )
        results = all_rows(
            conn,
            """SELECT id, job_id, kind, version, parent_result_id, outcome, content, context_snapshot_id
                 FROM gtm.stage_results WHERE lead_id = %s ORDER BY created_at""",
            (lead_id,),
        )
        result_ids = [r["id"] for r in results]
        citations = all_rows(
            conn,
            """SELECT id, research_result_id, key, source, url, quote, source_date, date_basis
                 FROM gtm.citations WHERE research_result_id = ANY(%s) ORDER BY key""",
            (result_ids,),
        )
        checks = all_rows(
            conn,
            """SELECT checked_result_id, citation_id, finding_key, identity, recency, meaning, passed, reasons
                 FROM gtm.citation_checks WHERE checked_result_id = ANY(%s) ORDER BY finding_key""",
            (result_ids,),
        )
        decisions = all_rows(
            conn,
            """SELECT checked_result_id, decision, service_area, property_need, reasons, proposed_fit
                 FROM gtm.qualification_decisions WHERE lead_id = %s""",
            (lead_id,),
        )
        job_ids = [j["id"] for j in jobs]
        snapshots = all_rows(
            conn,
            "SELECT id, job_id, role, payload FROM gtm.context_snapshots WHERE job_id = ANY(%s)",
            (job_ids,),
        )
        calls = all_rows(
            conn,
            """SELECT job_id, kind, operation, state, request_id, cost_settled_usd, cost_reserved_usd
                 FROM gtm.provider_calls WHERE job_id = ANY(%s) ORDER BY created_at, seq""",
            (job_ids,),
        )
        outreach = maybe_one(
            conn,
            "SELECT id, step, writing_result_id FROM gtm.outreach_items WHERE lead_id = %s",
            (lead_id,),
        )
    return {
        "lead_id": lead_id,
        "records": records,
        "states": states,
        "jobs": jobs,
        "results": results,
        "citations": citations,
        "checks": checks,
        "decisions": decisions,
        "snapshots": snapshots,
        "calls": calls,
        "outreach": outreach,
    }


def _short(value: Any) -> str:
    return str(value)[:8] if value else "-"


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def render_trace(t: dict[str, Any], *, show_snapshot_roles: tuple[str, ...] = ("writer",)) -> str:
    out: list[str] = []
    state = t["states"][0] if t["states"] else {"state": "?", "state_reason": None}
    reason = f" ({state['state_reason']})" if state["state_reason"] else ""
    out.append(f"Lead {t['lead_id']} - state: {state['state']}{reason}")
    for r in t["records"]:
        need = r["content"]["recorded_need"]
        out.append(
            f"  record v{r['version']} {_short(r['id'])}: {r['content']['full_name']}, lives in "
            f"{r['content']['residence_location']}; {need['category']} in {need['property_location'] or '(not given)'}; "
            f"recorded {need['recorded_at']} via {need['source']}"
        )
    results = {r["id"]: r for r in t["results"]}
    snapshots = {s["job_id"]: s for s in t["snapshots"]}
    for job in t["jobs"]:
        out.append("")
        pinned = f"config v{job['config_version']}"
        if job["model_id"]:
            pinned += f" | model {job['model_id']} | prompt {job['prompt_version']} sha {job['prompt_sha256'][:12]}"
        if job["input_result_id"]:
            parent = results.get(job["input_result_id"])
            kind = parent["kind"] if parent else "result"
            pinned += (
                f" | input {kind} {_short(job['input_result_id'])} v{job['input_result_version']}"
            )
        err = f" error={job['error_code']}" if job["error_code"] else ""
        out.append(
            f"[{job['stage']}] job {_short(job['id'])} {job['status']} (attempts {job['attempts']}){err}"
        )
        out.append(f"  key: {job['job_key']}")
        out.append(f"  pinned: {pinned}")
        job_calls = [c for c in t["calls"] if c["job_id"] == job["id"]]
        if job_calls:
            cost = sum(c["cost_settled_usd"] or 0 for c in job_calls)
            kinds = ", ".join(f"{c['operation']}:{c['state']}" for c in job_calls)
            out.append(f"  provider calls ({len(job_calls)}, ${cost:.6f}): {kinds}")
        snap = snapshots.get(job["id"])
        if snap and snap["role"] in show_snapshot_roles:
            out.append(f"  exact {snap['role']} input (context snapshot {_short(snap['id'])}):")
            user = next(m for m in snap["payload"]["messages"] if m["role"] == "user")
            out.extend("    " + line for line in _json(json.loads(user["content"])).splitlines())
        for res in (r for r in t["results"] if r["job_id"] == job["id"]):
            out.extend(_render_result(res, t))
        if job["stage"] == "draft":
            out.append("  draft operation: not implemented yet (next task); job stays queued")
    if t["outreach"]:
        o = t["outreach"]
        out.append("")
        out.append(
            f"outreach item {_short(o['id'])} ({o['step']}) <- writing {_short(o['writing_result_id'])}"
        )
    return "\n".join(out)


def _render_result(res: dict[str, Any], t: dict[str, Any]) -> list[str]:
    out = [
        f"  -> {res['kind']} v{res['version']} {_short(res['id'])}"
        + (f" outcome={res['outcome']}" if res["outcome"] else "")
    ]
    c = res["content"]
    if res["kind"] == "research":
        for cit in (x for x in t["citations"] if x["research_result_id"] == res["id"]):
            where = cit["url"] or "supplied record"
            out.append(
                f"     {cit['key']} {cit['source']} {where} ({cit['date_basis']} {cit['source_date']}): "
                f'"{cit["quote"]}"'
            )
        for i, f in enumerate(c["output"]["findings"], start=1):
            out.append(f"     F{i} [{f['use']}] {f['text']} <- {', '.join(f['citations'])}")
        out.append(f"     unknowns: {', '.join(c['output']['unknowns']) or 'none'}")
    elif res["kind"] == "checked_research":
        keys = {x["id"]: x["key"] for x in t["citations"]}
        for chk in (x for x in t["checks"] if x["checked_result_id"] == res["id"]):
            mark = "pass" if chk["passed"] else "FAIL " + ",".join(chk["reasons"])
            out.append(
                f"     {chk['finding_key']}/{keys.get(chk['citation_id'], '?')}: identity={chk['identity']} "
                f"recency={chk['recency']} meaning={chk['meaning']} -> {mark}"
            )
        d = next((x for x in t["decisions"] if x["checked_result_id"] == res["id"]), None)
        if d:
            why = f" ({', '.join(d['reasons'])})" if d["reasons"] else ""
            out.append(
                f"     decision (code): {d['decision']}{why}; service_area={d['service_area']} "
                f"property_need={d['property_need']}; researcher proposed {d['proposed_fit']['service_area']}/"
                f"{d['proposed_fit']['property_need']}"
            )
        for f in c["findings"]:
            out.append(f"     kept {f['id']} [{f['use']}]: {f['text']}")
        for f in c["excluded_findings"]:
            out.append(f"     excluded [{f['use']}]: {f['text']} ({', '.join(f['reasons'])})")
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
        for v in c["email_check"]["sentences"]:
            out.append(f"       {v['verdict']:<12} {v['text']}")
        for claim in o["claims"]:
            if claim["refs"]:
                out.append(f"     claim -> {', '.join(claim['refs'])}: {claim['sentence']}")
    return out
