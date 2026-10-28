"""Check stage: citations are not conclusions.

Code checks identity and recency for every citation. Meaning is checked by code for supplied
records and by a tool-free claim-check call for web claims. A finding survives only if every
citation it uses passes all three checks. Code, not the model, makes the qualification decision,
and service fit uses the requested property location, never the person's residence.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urlparse

from gtm.calls import CallRecorder
from gtm.context import ContextError, StageContext, StageInputs
from gtm.contracts import (
    CheckedFinding,
    CheckedResearch,
    ClaimCheckInput,
    ClaimCheckOutput,
    ClaimCheckPerson,
    ExcludedFinding,
    LeadRecord,
    OutreachContext,
    ResearchOutput,
    ServiceArea,
)
from gtm.queue import StageOutput
from gtm.util import canonical_json, estimate_tokens

# Words a recorded statement should contain for its need category to be consistent with it.
CATEGORY_TERMS = {
    "buy": ("buy", "buying", "purchase", "looking for", "home", "house", "condo"),
    "sell": ("sell", "selling", "sale", "list my", "listing"),
    "rent": ("rent", "renting", "lease", "leasing"),
}


def _city(location: str | None) -> str | None:
    if not location:
        return None
    return location.split(",")[0].strip().casefold() or None


def in_service_area(location: str, area: ServiceArea) -> bool:
    low = location.casefold()
    if any(re.search(rf"\b{re.escape(p.casefold())}\b", low) for p in area.places):
        return True
    return any(code in area.postal_codes for code in re.findall(r"\b\d{5}\b", location))


def identity_check(
    citation: dict[str, Any], record: LeadRecord, source_text: str
) -> tuple[str, list[str]]:
    if citation["source"] == "import_record":
        return "pass", []  # tied to the immutable supplied record and its brokerage provenance
    low = source_text.casefold()
    if record.full_name.casefold() not in low:
        return "fail", ["name_not_in_source"]
    seen = _city((citation.get("identifiers_seen") or {}).get("location"))
    known = {
        c
        for c in (_city(record.residence_location), _city(record.recorded_need.property_location))
        if c
    }
    if seen and seen not in known:
        return "fail", ["identity_conflict"]
    if record.organization and record.organization.casefold() in low:
        return "pass", []
    # A name plus a generic location is not conclusive.
    return "ambiguous", ["identity_insufficient_identifiers"]


def recency_check(
    citation: dict[str, Any], today: date, max_age_days: int
) -> tuple[str, list[str]]:
    raw = citation.get("source_date")
    if raw is None:
        return "undated", ["evidence_undated"]
    when = raw if isinstance(raw, date) else date.fromisoformat(raw)
    age = (today - when).days
    if age < 0:
        return "undated", ["evidence_future_dated"]
    if age > max_age_days:
        return "stale", ["evidence_stale"]
    return "pass", []


def record_meaning_check(citation: dict[str, Any], record: LeadRecord) -> tuple[str, list[str]]:
    note = re.sub(r"\s+", " ", record.recorded_need.note).casefold()
    if re.sub(r"\s+", " ", citation["quote"]).casefold() not in note:
        return "fail", ["quote_not_in_record"]
    verb = record.recorded_need.category.split("_")[0]
    terms = CATEGORY_TERMS.get(verb)
    if terms and not any(t in note for t in terms):
        return "unclear", ["category_statement_mismatch"]
    return "pass", []


def build_payload(inputs: StageInputs) -> dict[str, Any]:
    if inputs.input_result is None:
        raise ContextError("missing_parent")
    cfg = inputs.config
    rec = inputs.record
    research = ResearchOutput.model_validate(inputs.input_result.content["output"])
    sources = {s["url"]: s for s in inputs.input_result.content["sources"]}
    by_key = {c["key"]: c for c in inputs.citations}

    code: dict[str, dict[str, Any]] = {}
    for key, c in by_key.items():
        source = sources.get(c["url"] or "", {})
        text = source.get("text") or source.get("snippet") or c["quote"]
        identity, id_reasons = identity_check(c, rec, text)
        recency, date_reasons = recency_check(c, inputs.today, cfg.campaign.evidence_max_age_days)
        code[key] = {"identity": identity, "recency": recency, "reasons": id_reasons + date_reasons}

    person = ClaimCheckPerson(
        name=rec.full_name, location=rec.residence_location, organization=rec.organization
    )
    requests: list[dict[str, Any]] = []
    for index, finding in enumerate(research.findings):
        for key in finding.citations:
            c = by_key[key]
            if (
                c["source"] != "web"
                or code[key]["identity"] != "pass"
                or code[key]["recency"] != "pass"
            ):
                continue
            if len(requests) >= cfg.limits.check.web_claims:
                break
            title = sources.get(c["url"], {}).get("title", "")
            claim = ClaimCheckInput(
                claim=finding.text,
                quote=c["quote"],
                published_at=c["source_date"],
                source_provenance=f"web page on {urlparse(c['url']).netloc}: {title}"[:200],
                person=person,
            )
            messages = [
                {"role": "system", "content": cfg.prompts["claim_check"].text},
                {"role": "user", "content": canonical_json(claim.model_dump(mode="json"))},
            ]
            if estimate_tokens({"messages": messages}) > cfg.limits.check.input_tokens:
                raise ContextError("context_too_large")
            requests.append({"finding_index": index, "citation_key": key, "messages": messages})
    return {"as_of": inputs.today.isoformat(), "code_checks": code, "claim_requests": requests}


def execute(ctx: StageContext, calls: CallRecorder) -> StageOutput:
    inputs = ctx.inputs
    if inputs.input_result is None:
        raise ContextError("missing_parent")
    cfg = inputs.config
    rec = inputs.record
    research = ResearchOutput.model_validate(inputs.input_result.content["output"])
    by_key = {c["key"]: c for c in inputs.citations}
    code: dict[str, dict[str, Any]] = ctx.payload["code_checks"]

    meaning: dict[tuple[int, str], tuple[str, list[str]]] = {}
    repairs_left = cfg.limits.check.json_repairs
    for n, req in enumerate(ctx.payload["claim_requests"], start=1):
        out, used = calls.model_json(
            "claim_check", req["messages"], ClaimCheckOutput, round_no=n, repairs=repairs_left
        )
        repairs_left -= used
        if out.about_person == "yes" and out.supports_claim == "yes":
            meaning[(req["finding_index"], req["citation_key"])] = ("pass", [])
        elif "no" in (out.about_person, out.supports_claim):
            meaning[(req["finding_index"], req["citation_key"])] = (
                "fail",
                ["claim_check_rejected"],
            )
        else:
            meaning[(req["finding_index"], req["citation_key"])] = (
                "unclear",
                ["claim_check_unclear"],
            )

    checks: list[dict[str, Any]] = []
    passing: list[tuple[int, Any]] = []
    excluded: list[ExcludedFinding] = []
    failed_qualification_reasons: list[str] = []
    for index, finding in enumerate(research.findings):
        finding_ok = True
        finding_reasons: list[str] = []
        for key in finding.citations:
            c = by_key[key]
            if c["source"] == "import_record":
                m_status, m_reasons = record_meaning_check(c, rec)
                method = {
                    "identity": "supplied_record",
                    "recency": "code",
                    "meaning": "code_record",
                }
            else:
                m_status, m_reasons = meaning.get(
                    (index, key), ("not_checked", ["claim_not_checked"])
                )
                method = {"identity": "code", "recency": "code", "meaning": "model_claim_check"}
            passed = (
                code[key]["identity"] == "pass"
                and code[key]["recency"] == "pass"
                and m_status == "pass"
            )
            reasons = code[key]["reasons"] + ([] if m_status == "pass" else m_reasons)
            checks.append(
                {
                    "citation_id": c["id"],
                    "finding_key": f"F{index + 1}",
                    "identity": code[key]["identity"],
                    "recency": code[key]["recency"],
                    "meaning": m_status,
                    "method": method,
                    "passed": passed,
                    "reasons": reasons,
                }
            )
            if not passed:
                finding_ok = False
                finding_reasons.extend(r for r in reasons if r not in finding_reasons)
        if finding_ok:
            passing.append((index, finding))
        else:
            excluded.append(
                ExcludedFinding(text=finding.text, use=finding.use, reasons=finding_reasons)
            )
            if finding.use == "qualification":
                failed_qualification_reasons.extend(finding_reasons)

    decision, service_area, property_need, reasons = decide(
        rec,
        cfg.campaign.service_area,
        cfg.campaign.need_categories,
        research,
        passing,
        by_key,
        failed_qualification_reasons,
    )

    checked_findings = []
    for n, (_index, finding) in enumerate(passing, start=1):
        cits = [by_key[k] for k in finding.citations]
        dates = [
            date.fromisoformat(str(c["source_date"])) for c in cits if c["source_date"] is not None
        ]
        provenance = ", ".join(
            f"brokerage_supplied:{rec.recorded_need.source}"
            if c["source"] == "import_record"
            else f"web:{urlparse(c['url']).netloc}"
            for c in cits
        )
        checked_findings.append(
            CheckedFinding(
                id=f"finding_{n}",
                text=finding.text,
                use=finding.use,
                citation_ids=[c["id"] for c in cits],
                source_date=min(dates) if dates else None,
                provenance=provenance,
            )
        )

    checked = CheckedResearch(
        research_result_id=inputs.input_result.id,
        qualification=decision,
        service_area=service_area,
        property_need=property_need,
        reasons=reasons,
        findings=checked_findings,
        excluded_findings=excluded,
        unknowns=research.unknowns,
        contradictions=[c.text for c in research.contradictions],
        outreach_context=OutreachContext(may_mention_inquiry=rec.may_mention_inquiry),
    )
    qualified = decision == "qualified"
    return StageOutput(
        kind="checked_research",
        schema_version="checked_research.v1",
        content=checked.model_dump(mode="json"),
        outcome=decision,
        lead_state="in_progress" if qualified else decision,
        lead_state_reason=None if qualified else (reasons[0] if reasons else None),
        next_stage="write" if qualified else None,
        citation_checks=checks,
        decision={
            "decision": decision,
            "service_area": service_area,
            "property_need": property_need,
            "reasons": reasons,
            "proposed_fit": research.proposed_fit.model_dump(mode="json"),
        },
    )


def decide(
    record: LeadRecord,
    area: ServiceArea,
    need_categories: list[str],
    research: ResearchOutput,
    passing: list[tuple[int, Any]],
    by_key: dict[str, dict[str, Any]],
    failed_qualification_reasons: list[str],
) -> tuple[Any, Any, Any, list[str]]:
    """Return (decision, service_area, property_need, reasons). Both criteria need checked support."""
    need = record.recorded_need
    location = (need.property_location or "").strip()
    service_area = (
        "unknown" if not location else ("met" if in_service_area(location, area) else "not_met")
    )
    property_need = "met" if need.category in need_categories else "not_met"

    if research.contradictions:
        return "needs_review", service_area, property_need, ["contradictions_reported"]
    supported = any(
        f.use == "qualification"
        and any(by_key[k]["source"] == "import_record" for k in f.citations)
        for _i, f in passing
    )
    if not supported:
        reasons = sorted(set(failed_qualification_reasons)) or ["no_checked_qualification_evidence"]
        review = {
            "identity_insufficient_identifiers",
            "claim_check_unclear",
            "category_statement_mismatch",
        }
        if any(r in review for r in reasons):
            return "needs_review", "unknown", "unknown", reasons
        return "insufficient_evidence", "unknown", "unknown", reasons
    if service_area == "unknown":
        return "insufficient_evidence", service_area, property_need, ["property_location_missing"]
    if service_area == "not_met":
        return (
            "disqualified",
            service_area,
            property_need,
            ["property_location_outside_service_area"],
        )
    if property_need == "not_met":
        return "disqualified", service_area, property_need, ["need_category_not_offered"]
    return "qualified", service_area, property_need, []
