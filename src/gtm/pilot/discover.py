"""Discovery: a Venice-hosted model finds seller opportunities in live web search results.

Venice runs the web search itself (``enable_web_search: "on"``) and returns the results it used
as citations. Code marks which evidence URLs are among those citations and whether each quote
appears in the result's text; the verify stage rejects anything else. A response with no
citations means web search didn't happen, so the run stops rather than trusting model memory.

Every request is saved before it is sent and every response after, under ``raw/``. Each
request's actual cost is logged from Venice's reported token usage. Resuming a run reuses the
saved responses, so a search that already succeeded is never paid for twice.

Without ``execute=True`` nothing is sent: the plan shows the exact requests and a worst-case cost.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from gtm.pilot.common import (
    REPO_ROOT,
    append_cost,
    new_run_dir,
    norm_text,
    norm_url,
    opportunity_id,
    read_costs,
    read_json,
    rebuild_json,
    utc_now,
    write_new_json,
)
from gtm.pilot.models import (
    Citation,
    Criteria,
    Discovered,
    DiscoveryOut,
    EvidenceCheck,
    Pricing,
    Provenance,
    SearchTask,
)
from gtm.util import estimate_tokens, sha256_hex

VENICE_CHAT_URL = "https://api.venice.ai/api/v1/chat/completions"
PROMPT_VERSION = "discovery-v1"
PROMPT_FILE = REPO_ROOT / "pilot" / "prompts" / f"{PROMPT_VERSION}.md"
# Room for the search results Venice adds to a request. Used only for the worst-case estimate;
# actual cost comes from the usage Venice reports.
SEARCH_CONTEXT_TOKENS = 30_000

Post = Callable[[str, dict[str, Any], str], tuple[int, dict[str, Any]]]


def http_post(url: str, payload: dict[str, Any], api_key: str) -> tuple[int, dict[str, Any]]:
    """HTTP status and JSON body. Status 0 means no response (the request may still be billed)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body: dict[str, Any] = json.loads(response.read().decode())
            return int(response.status), body
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        try:
            parsed: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError:
            parsed = {"error": text[:500]}
        return int(exc.code), parsed
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"error": type(exc).__name__}


def load_venice_key() -> str:
    """VENICE_API_KEY if set, else the key saved in Pi's credential file. Never printed."""
    env = os.environ.get("VENICE_API_KEY")
    if env:
        return env
    path = Path.home() / ".pi" / "agent" / "auth.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    key = (data.get("venice") or {}).get("key")
    if not key:
        raise SystemExit(
            "No Venice key found. On the VM run gtm-save-venice-key, or set VENICE_API_KEY."
        )
    return str(key)


def system_prompt(criteria: Criteria) -> str:
    template = PROMPT_FILE.read_text()
    signals = "\n".join(
        f"- `{name}`: {rule.description}" for name, rule in criteria.signals.items() if rule.enabled
    )
    return (
        template.replace("{{MARKET}}", criteria.market)
        .replace("{{AREAS}}", ", ".join(criteria.areas))
        .replace("{{PRIORITY_AREAS}}", ", ".join(criteria.priority_areas) or "none")
        .replace("{{SIGNALS}}", signals)
    )


def build_request(
    criteria: Criteria, task: SearchTask, prompt: str, today: date, wanted: int
) -> dict[str, Any]:
    user = (
        f"Search task: {task.task}\n"
        f"Signal type to look for: {task.signal}.\n"
        f"Today is {today.isoformat()}. Prefer sources from the last {criteria.lookback_days} days.\n"
        f"Return at most {wanted} candidates."
    )
    return {
        "model": criteria.model,
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
        "max_tokens": criteria.max_output_tokens,
        "reasoning_effort": criteria.reasoning_effort,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "discovery",
                "strict": False,
                "schema": DiscoveryOut.model_json_schema(),
            },
        },
        "venice_parameters": {
            "enable_web_search": "on",
            "enable_web_citations": True,
            "include_venice_system_prompt": False,
        },
    }


def worst_case_usd(payload: dict[str, Any], pricing: Pricing) -> float:
    tokens_in = estimate_tokens(payload["messages"]) + SEARCH_CONTEXT_TOKENS
    return (
        tokens_in * pricing.input_usd_per_mtok / 1e6
        + int(payload["max_tokens"]) * pricing.output_usd_per_mtok / 1e6
        + pricing.web_search_usd_per_request
    )


def actual_usd(usage: dict[str, Any], pricing: Pricing) -> float:
    """Reported tokens at list price (cache discounts ignored, so this errs high), plus the search."""
    return (
        int(usage.get("prompt_tokens", 0)) * pricing.input_usd_per_mtok / 1e6
        + int(usage.get("completion_tokens", 0)) * pricing.output_usd_per_mtok / 1e6
        + pricing.web_search_usd_per_request
    )


def strip_to_json(content: str) -> str:
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", content, flags=re.S)
    if fenced:
        content = fenced.group(1).strip()
    start, end = content.find("{"), content.rfind("}")
    return content[start : end + 1] if start != -1 and end > start else content


def citations_of(body: dict[str, Any]) -> list[Citation]:
    raw = (body.get("venice_parameters") or {}).get("web_search_citations") or []
    return [Citation.model_validate(c) for c in raw if isinstance(c, dict) and c.get("url")]


def parse_response(
    body: dict[str, Any],
    task: SearchTask,
    run_id: str,
    raw_file: str,
    model: str,
    request_usd: float,
) -> tuple[list[Discovered], list[str]]:
    by_url = {norm_url(c.url): c for c in citations_of(body)}
    choices = body.get("choices") or []
    if not choices:
        return [], [f"{task.id}: response has no choices"]
    content = (choices[0].get("message") or {}).get("content") or ""
    try:
        out = DiscoveryOut.model_validate_json(strip_to_json(content))
    except ValidationError as exc:
        return [], [
            f"{task.id}: model output did not match the schema ({exc.error_count()} errors)"
        ]

    records: list[Discovered] = []
    for cand in out.candidates:
        checks = []
        for ev in cand.evidence:
            cit = by_url.get(norm_url(ev.url))
            checks.append(
                EvidenceCheck(
                    url=ev.url,
                    url_in_search_results=cit is not None,
                    quote_in_search_result=bool(
                        cit and cit.content and norm_text(ev.quote) in norm_text(cit.content)
                    ),
                    search_result_title=cit.title if cit else None,
                    search_result_date=cit.date if cit else None,
                )
            )
        records.append(
            Discovered(
                opportunity_id=opportunity_id(
                    cand.property.address, cand.property.city, cand.evidence[0].url
                ),
                signal=cand.signal,
                summary=cand.summary,
                property=cand.property,
                representation=cand.representation,
                evidence=cand.evidence,
                missing_info=cand.missing_info,
                discovery_checks=checks,
                provenance=Provenance(
                    run_id=run_id,
                    search_id=task.id,
                    request_id=body.get("id"),
                    model=str(body.get("model") or model),
                    prompt_version=PROMPT_VERSION,
                    raw_response_file=raw_file,
                    retrieved_at=utc_now(),
                    request_usd=round(request_usd, 6),
                ),
            )
        )
    return records, []


@dataclass
class PlannedRequest:
    task: SearchTask
    payload: dict[str, Any]
    worst_case_usd: float
    done: bool = False  # a saved successful response exists (resume only)


@dataclass
class DiscoveryResult:
    planned: list[PlannedRequest]
    run_dir: Path | None = None
    requests_sent: int = 0
    reused: int = 0
    candidates: int = 0
    spent_usd: float = 0.0  # the whole run, including earlier invocations
    stopped_reason: str | None = None
    pending: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def plan(criteria: Criteria, today: date, only: list[str] | None = None) -> list[PlannedRequest]:
    known = {t.id for t in criteria.searches}
    if unknown := sorted(set(only or []) - known):
        raise ValueError(f"unknown search ids: {unknown}; choose from {sorted(known)}")
    prompt = system_prompt(criteria)
    planned = []
    for task in criteria.searches:
        if not criteria.signals[task.signal].enabled or (only and task.id not in only):
            continue
        payload = build_request(
            criteria, task, prompt, today, min(criteria.max_per_search, criteria.max_candidates)
        )
        planned.append(PlannedRequest(task, payload, worst_case_usd(payload, criteria.pricing)))
    return planned


def _attempts(run_dir: Path, task_id: str) -> list[int]:
    return sorted(
        int(m.group(1))
        for p in (run_dir / "raw").glob(f"discovery-{task_id}-*.request.json")
        if (m := re.search(r"-(\d+)\.request\.json$", p.name))
    )


def _saved_success(run_dir: Path, task_id: str) -> tuple[str, dict[str, Any]] | None:
    for n in reversed(_attempts(run_dir, task_id)):
        raw_file = f"raw/discovery-{task_id}-{n}.json"
        if (run_dir / raw_file).exists():
            saved = read_json(run_dir / raw_file)
            if saved["http_status"] == 200:
                return raw_file, saved["response"]
    return None


def open_run(run_dir: Path) -> tuple[Criteria, date, dict[str, Any]]:
    """The criteria and date a run started with, so a resumed run sends identical requests."""
    meta = read_json(run_dir / "run.json")
    if (run_dir / "verified.json").exists():
        raise SystemExit(
            f"{run_dir} has been verified already; start a new run instead of resuming it."
        )
    criteria = Criteria.model_validate(meta["criteria"])
    if sha256_hex(system_prompt(criteria)) != meta["prompt_sha256"]:
        raise SystemExit(
            f"{PROMPT_FILE.name} changed since {run_dir.name} started; start a new run."
        )
    return criteria, date.fromisoformat(meta["today"]), meta


def run_discovery(
    criteria: Criteria,
    criteria_text: str,
    data_dir: Path,
    label: str,
    *,
    execute: bool,
    max_usd: float,
    today: date,
    only: list[str] | None = None,
    resume: Path | None = None,
    post: Post = http_post,
    api_key: str | None = None,
) -> DiscoveryResult:
    if resume:
        criteria, today, _ = open_run(resume)
    planned = plan(criteria, today, only)
    if resume:
        for item in planned:
            item.done = _saved_success(resume, item.task.id) is not None
    result = DiscoveryResult(planned=planned, run_dir=resume)
    if not execute:
        return result

    key = api_key or load_venice_key()
    prompt = system_prompt(criteria)
    if resume:
        run_dir = resume
    else:
        run_dir = new_run_dir(data_dir, label)
        write_new_json(
            run_dir / "run.json",
            {
                "run_id": run_dir.name,
                "started_at": utc_now(),
                "today": today.isoformat(),
                "prompt_version": PROMPT_VERSION,
                "prompt_sha256": sha256_hex(prompt),
                "criteria_sha256": sha256_hex(criteria_text),
                "criteria": criteria.model_dump(mode="json"),
            },
        )
    result.run_dir = run_dir
    run_id = run_dir.name
    _charge_unanswered(run_dir, criteria)
    costs = read_costs(run_dir)
    result.spent_usd = sum(float(c.get("usd") or 0) for c in costs)
    cost_by_file = {c.get("raw_file"): float(c.get("usd") or 0) for c in costs}

    records: list[Discovered] = []
    ids: set[str] = set()
    for item in planned:
        remaining = criteria.max_candidates - len(ids)
        if remaining <= 0:
            result.stopped_reason = "enough_candidates"
            break
        saved = _saved_success(run_dir, item.task.id)
        if saved:
            raw_file, body = saved
            cost = cost_by_file.get(raw_file, 0.0)
            result.reused += 1
        else:
            payload = build_request(
                criteria, item.task, prompt, today, min(criteria.max_per_search, remaining)
            )
            estimate = worst_case_usd(payload, criteria.pricing)
            if result.spent_usd + estimate > max_usd:
                result.stopped_reason = "budget"
                break
            n = max(_attempts(run_dir, item.task.id), default=0) + 1
            raw_file = f"raw/discovery-{item.task.id}-{n}.json"
            write_new_json(
                run_dir / f"raw/discovery-{item.task.id}-{n}.request.json",
                {"sent_at": utc_now(), "worst_case_usd": round(estimate, 6), "request": payload},
            )
            status, body = post(VENICE_CHAT_URL, payload, key)
            result.requests_sent += 1
            write_new_json(
                run_dir / raw_file,
                {"received_at": utc_now(), "http_status": status, "response": body},
            )
            usage = body.get("usage") if status == 200 else None
            cost = actual_usd(usage, criteria.pricing) if usage else estimate
            result.spent_usd += cost
            append_cost(
                run_dir,
                _cost_entry(item.task.id, raw_file, criteria.model, status, body, usage, cost),
            )
            if status != 200:
                result.problems.append(
                    f"{item.task.id}: Venice returned HTTP {status or 'no response'}"
                )
                continue
        if not citations_of(body):
            result.problems.append(f"{item.task.id}: the response has no web search results")
            result.stopped_reason = "no_web_sources"
            break
        found, problems = parse_response(body, item.task, run_id, raw_file, criteria.model, cost)
        result.problems.extend(problems)
        for rec in found:
            # Same address and city (or same main URL) as an earlier candidate: one record per ID.
            if rec.opportunity_id in ids:
                result.problems.append(
                    f"{item.task.id}: {rec.opportunity_id} repeats an earlier candidate"
                )
                continue
            if len(ids) >= criteria.max_candidates:
                result.problems.append(
                    f"{item.task.id}: dropped {rec.opportunity_id}, over max_candidates"
                )
                continue
            ids.add(rec.opportunity_id)
            records.append(rec)

    all_planned = plan(criteria, today)
    result.pending = [p.task.id for p in all_planned if _saved_success(run_dir, p.task.id) is None]
    result.candidates = len(ids)
    rebuild_json(run_dir / "discovered.json", [r.model_dump(mode="json") for r in records])
    rebuild_json(
        run_dir / "discovery-summary.json",
        {
            "updated_at": utc_now(),
            "requests_sent_total": len(read_costs(run_dir)),
            "candidates": result.candidates,
            "spent_usd": round(result.spent_usd, 6),
            "stopped_reason": result.stopped_reason,
            "searches_pending": result.pending,
            "problems": result.problems,
        },
    )
    return result


def _cost_entry(
    task_id: str,
    raw_file: str,
    model: str,
    status: int,
    body: dict[str, Any],
    usage: dict[str, Any] | None,
    cost: float,
) -> dict[str, Any]:
    return {
        "at": utc_now(),
        "stage": "discovery",
        "search_id": task_id,
        "raw_file": raw_file,
        "provider": "venice",
        "model": model,
        "request_id": body.get("id"),
        "http_status": status,
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "completion_tokens": (usage or {}).get("completion_tokens"),
        "reasoning_tokens": ((usage or {}).get("completion_tokens_details") or {}).get(
            "reasoning_tokens"
        ),
        "web_searches": 1,
        "usd": round(cost, 6),
        "usd_basis": "reported_usage" if usage else "worst_case_estimate",
    }


def _charge_unanswered(run_dir: Path, criteria: Criteria) -> None:
    """A request saved without a response (the program stopped mid-call) may have been billed."""
    logged = {c.get("raw_file") for c in read_costs(run_dir)}
    for req in sorted((run_dir / "raw").glob("discovery-*.request.json")):
        raw_file = f"raw/{req.name.removesuffix('.request.json')}.json"
        if raw_file in logged or (run_dir / raw_file).exists():
            continue
        saved = read_json(req)
        task_id = re.sub(
            r"-\d+$", "", req.name.removesuffix(".request.json").removeprefix("discovery-")
        )
        entry = _cost_entry(
            task_id, raw_file, criteria.model, 0, {}, None, float(saved["worst_case_usd"])
        )
        append_cost(run_dir, {**entry, "usd_basis": "worst_case_estimate_no_response"})
