"""Finished-email checks: code rules first, then a tool-free sentence check. Failures stop before Gmail."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gtm.contracts import WriterInput, WriterOutput
from gtm.providers.base import ModelRequest, ModelResponse, Usage
from gtm.validation import email_code_checks
from tests.conftest import JORDAN, Env

WRITER_INPUT = WriterInput.model_validate(
    {
        "recipient": {"first_name": "Jordan", "language_preference": None},
        "findings": [
            {
                "id": "finding_1",
                "text": "Asked for help to buy a home in Torrance, CA (website inquiry, 2026-10-20).",
                "refs": ["c1"],
                "recorded_at": "2026-10-20",
            }
        ],
        "unknowns": ["Budget", "Purchase timeline"],
        "contradictions": [],
        "sender": {
            "name": "Taylor Morgan",
            "title": "Broker",
            "brokerage": "Example Homes",
            "facts": [
                {
                    "id": "sender_fact_1",
                    "text": "Taylor Morgan helps buyers find homes across Torrance.",
                }
            ],
        },
        "campaign": {
            "offer": "A short call.",
            "cta": "Would a brief call this week be useful?",
            "tone_rules": [],
            "tone_reference": "",
            "max_words": 140,
        },
        "outreach_context": {"may_mention_inquiry": False},
    }
)
CLEAN = "Hi Jordan,\n\nTaylor Morgan helps buyers find homes across Torrance.\n\nWould a brief call this week be useful?"


def _email(
    body: str, claims: list[dict[str, Any]] | None = None, language: str = "en"
) -> WriterOutput:
    return WriterOutput(
        subject="A home search in Torrance",
        body=body,
        language=language,
        claims=claims
        or [
            {
                "sentence": "Taylor Morgan helps buyers find homes across Torrance.",
                "refs": ["sender_fact_1"],
            }
        ],
    )


def _rules(out: WriterOutput) -> set[str]:
    return {
        f["rule"]
        for f in email_code_checks(out, WRITER_INPUT, language="en", allowed_contacts=set())
    }


def test_clean_email_passes() -> None:
    assert _rules(_email(CLEAN)) == set()


@pytest.mark.parametrize(
    ("extra", "rule"),
    [
        ("Homes near $850,000 are moving quickly.", "mentions_unknown"),
        ("Homes near $850,000 are moving quickly.", "unsupported_figure_or_date"),
        ("Since you plan to buy by spring, let's start now.", "unsupported_figure_or_date"),
        ("What is your timeline for moving?", "mentions_unknown"),
        ("As we discussed, I can help.", "implied_prior_relationship"),
        ("I'm following up on our chat.", "implied_prior_relationship"),
        ("It is a great area for your family.", "protected_attribute"),
        ("Thanks for your inquiry.", "inquiry_not_permitted"),
        ("See https://homes.example/listing for ideas.", "link_not_configured"),
        ("Call me at 310-555-0100.", "phone_not_configured"),
        ("Write to someone@else.example anytime.", "email_not_configured"),
    ],
)
def test_code_checks_catch_invented_or_prohibited_content(extra: str, rule: str) -> None:
    assert rule in _rules(_email(CLEAN + "\n\n" + extra))


def test_reference_language_and_length_rules() -> None:
    bad_ref = _email(
        CLEAN,
        [
            {
                "sentence": "Taylor Morgan helps buyers find homes across Torrance.",
                "refs": ["finding_9"],
            }
        ],
    )
    assert "unknown_reference" in _rules(bad_ref)
    missing = _email(CLEAN, [{"sentence": "A sentence that is not in the email.", "refs": []}])
    assert "claim_not_in_email" in _rules(missing)
    assert "language" in _rules(_email(CLEAN, language="es"))
    assert "max_words" in _rules(_email(CLEAN + " word" * 150))


def _writer_saying(sentence: str, refs: list[str]) -> Any:
    def respond(request: ModelRequest) -> ModelResponse:
        data = json.loads(request.messages[1]["content"])
        body = f"Hi {data['recipient']['first_name']},\n\n{sentence}\n\n{data['campaign']['cta']}"
        out = {
            "subject": "Your home search",
            "body": body,
            "language": "en",
            "claims": [{"sentence": sentence, "refs": refs}],
        }
        return ModelResponse(json.dumps(out), [], Usage(100, 50), "w")

    return respond


def test_invented_budget_stops_before_any_draft(jordan_run: Env) -> None:
    env = jordan_run
    env.model.script(
        "writer", _writer_saying("Your budget of $900,000 fits Torrance well.", ["finding_1"])
    )
    w = env.worker()
    w.run()
    w.close()
    writing = env.rows("SELECT outcome, content FROM gtm.stage_results WHERE kind = 'writing'")[0]
    assert writing["outcome"] == "needs_review"
    assert (
        writing["content"]["email_check"]["status"] == "skipped"
    )  # code failure; no model call needed
    assert "Your budget of $900,000 fits Torrance well." in writing["content"]["failing_sentences"]
    assert (
        env.scalar("SELECT count(*) FROM gtm.provider_calls WHERE operation = 'email_check'") == 0
    )
    assert env.scalar("SELECT count(*) FROM gtm.outreach_items") == 0
    assert env.lead_state(JORDAN["email"]) == (
        "needs_review",
        "email_checks_failed",
    )


def test_overstated_sentence_from_the_model_check_stops_before_any_draft(jordan_run: Env) -> None:
    env = jordan_run
    sentence = "You are ready to make an offer on a Torrance home."
    env.model.script("writer", _writer_saying(sentence, ["finding_1"]))

    def overstated(request: ModelRequest) -> ModelResponse:
        data = json.loads(request.messages[1]["content"])
        verdicts = [
            {"text": s, "verdict": "overstated" if s == sentence else "not_factual", "note": ""}
            for s in data["sentences"]
        ]
        return ModelResponse(json.dumps({"sentences": verdicts}), [], Usage(100, 50), "e")

    env.model.script("email_check", overstated)
    w = env.worker()
    w.run()
    w.close()
    writing = env.rows("SELECT outcome, content FROM gtm.stage_results WHERE kind = 'writing'")[0]
    assert writing["outcome"] == "needs_review"
    assert writing["content"]["failing_sentences"] == [sentence]
    assert env.scalar("SELECT count(*) FROM gtm.jobs WHERE stage = 'draft'") == 0
