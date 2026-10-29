"""Write stage: the writer's context is built from the exact checked result the job pins.

The writer sees checked findings (never page text, never the email address), sender facts
marked for outreach, and campaign tone rules. The finished email must pass code checks and a
tool-free email check before a draft operation is scheduled. M1 makes no automatic rewrite.
"""

from __future__ import annotations

from typing import Any

from gtm.calls import CallRecorder
from gtm.context import ContextError, StageContext, StageInputs
from gtm.contracts import (
    CheckedResearch,
    EmailCheckInput,
    EmailCheckOutput,
    SenderFact,
    TextRef,
    WriterCampaign,
    WriterFinding,
    WriterInput,
    WriterOutput,
    WriterRecipient,
    WriterSender,
)
from gtm.queue import StageOutput
from gtm.util import canonical_json, estimate_tokens
from gtm.validation import email_code_checks, split_sentences

MAX_FINDINGS = 5


def build_payload(inputs: StageInputs) -> dict[str, Any]:
    if inputs.input_result is None:
        raise ContextError("missing_parent")
    cfg = inputs.config
    rec = inputs.record
    checked = CheckedResearch.model_validate(inputs.input_result.content)
    if checked.qualification != "qualified":
        raise ContextError("input_not_qualified")

    # Qualification findings first. Findings are selected whole, never shortened.
    ordered = sorted(checked.findings, key=lambda f: 0 if f.use == "qualification" else 1)
    selected, omitted = ordered[:MAX_FINDINGS], ordered[MAX_FINDINGS:]
    writer_input = WriterInput(
        recipient=WriterRecipient(
            first_name=rec.full_name.split()[0], language_preference=rec.language_preference
        ),
        findings=[
            WriterFinding(
                id=f.id,
                text=f.text,
                refs=f.citation_keys,
                recorded_at=f.source_date,
            )
            for f in selected
        ],
        unknowns=checked.unknowns,
        contradictions=checked.contradictions,
        sender=WriterSender(
            name=cfg.sender.name,
            title=cfg.sender.title,
            brokerage=cfg.brokerage.name,
            facts=[SenderFact(id=f.id, text=f.text) for f in cfg.sender.facts if f.use_in_outreach],
        ),
        campaign=WriterCampaign(
            offer=cfg.campaign.offer,
            cta=cfg.campaign.cta,
            tone_rules=cfg.campaign.tone_rules,
            tone_reference=cfg.campaign.tone_reference,
            max_words=cfg.campaign.max_words,
        ),
        outreach_context=checked.outreach_context,
    )
    messages = [
        {"role": "system", "content": cfg.prompts["writer"].text},
        {"role": "user", "content": canonical_json(writer_input.model_dump(mode="json"))},
    ]
    if estimate_tokens({"messages": messages}) > cfg.limits.write.input_tokens:
        raise ContextError("context_too_large")
    return {
        "messages": messages,
        "writer_input": writer_input.model_dump(mode="json"),
        "omitted_findings": [f.id for f in omitted],
    }


def execute(ctx: StageContext, calls: CallRecorder) -> StageOutput:
    cfg = ctx.inputs.config
    wi = WriterInput.model_validate(ctx.payload["writer_input"])
    repairs = cfg.limits.write.json_repairs

    out, used = calls.model_json(
        "writer", ctx.payload["messages"], WriterOutput, round_no=1, repairs=repairs
    )
    sentences = [out.subject, *split_sentences(out.body)]
    language = wi.recipient.language_preference or cfg.campaign.language
    failures = email_code_checks(out, wi, language=language, allowed_contacts=set())

    email_check: dict[str, Any] = {
        "status": "skipped",
        "reason": "code_checks_failed",
        "sentences": [],
    }
    flagged: list[dict[str, Any]] = []
    if not failures:
        check_input = EmailCheckInput(
            subject=out.subject,
            body=out.body,
            sentences=sentences,
            claims=out.claims,
            findings=[TextRef(id=f.id, text=f.text) for f in wi.findings],
            sender_facts=[TextRef(id=f.id, text=f.text) for f in wi.sender.facts],
            unknowns=wi.unknowns,
            outreach_context=wi.outreach_context,
        )
        messages = [
            {"role": "system", "content": cfg.prompts["email_check"].text},
            {"role": "user", "content": canonical_json(check_input.model_dump(mode="json"))},
        ]

        def one_per_sentence(o: EmailCheckOutput) -> list[str]:
            if [v.text for v in o.sentences] != sentences:
                return ["sentences must match the input sentences one-to-one and in order"]
            return []

        verdicts, _ = calls.model_json(
            "email_check",
            messages,
            EmailCheckOutput,
            round_no=2 + used,
            repairs=repairs,
            validate=one_per_sentence,
        )
        email_check = {
            "status": "completed",
            "sentences": [v.model_dump(mode="json") for v in verdicts.sentences],
        }
        flagged = [
            v.model_dump(mode="json")
            for v in verdicts.sentences
            if v.verdict in ("unsupported", "overstated")
        ]

    passed = not failures and not flagged
    content = {
        "output": out.model_dump(mode="json"),
        "sentences": sentences,
        "code_checks": {"passed": not failures, "failures": failures},
        "email_check": email_check,
        "failing_sentences": sorted(
            {f["sentence"] for f in failures if f["sentence"]} | {v["text"] for v in flagged}
        ),
    }
    return StageOutput(
        kind="writing",
        schema_version="writing.v1",
        content=content,
        outcome="passed" if passed else "needs_review",
        lead_state="ready_for_draft" if passed else "needs_review",
        lead_state_reason=None if passed else "email_checks_failed",
        next_stage="draft" if passed else None,
    )
