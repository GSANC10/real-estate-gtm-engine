"""Code checks for a finished email (§2.4). Any failure stops the lead at needs_review.

The model email check can fail an email but can never clear a failure found here.
"""

from __future__ import annotations

import re
from typing import Any

from gtm.contracts import WriterInput, WriterOutput

URL_RE = re.compile(r"(https?://\S+|www\.\S+|\b[\w-]+\.(?:com|net|org|io|co|us|example)\b)", re.I)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")

# "May" is matched only with a day number, so the ordinary word "may" is not a date.
MONTHS = (
    r"january|february|march|april|june|july|august|september|october|november|december|"
    r"jan|feb|apr|jun|jul|aug|sept|oct|nov"
)
FIGURE_RES = (
    re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:k|m|million|thousand)?\b", re.I),
    re.compile(r"\b\d+(?:\.\d+)?\s?%"),
    re.compile(r"\b\d[\d,]*(?:\.\d+)?\b"),
    re.compile(rf"\b(?:{MONTHS})\b", re.I),
    re.compile(r"\bMay\s+\d{1,2}\b"),
    re.compile(
        r"\b(?:next|this|last|within|by|in|early|late)\s+(?:the\s+)?"
        r"(?:week|weekend|month|year|spring|summer|fall|autumn|winter|quarter)s?\b",
        re.I,
    ),
    re.compile(r"\b(?:soon|asap|right away|immediately|tomorrow|today)\b", re.I),
)

UNKNOWN_TOPICS: dict[str, re.Pattern[str]] = {
    "budget": re.compile(
        r"\b(?:budget|price range|afford\w*|pre-?approv\w*|down payment|mortgage|financing)\b|\$\s?\d",
        re.I,
    ),
    "timeline": re.compile(
        r"\b(?:timeline|time ?frame|move-?in date|moving date|closing date|"
        r"ready to (?:buy|move|sell)|plan(?:ning)? to (?:buy|move|sell) (?:by|in|this|next))\b",
        re.I,
    ),
    "agent": re.compile(
        r"\b(?:your (?:current )?agent|another agent|other agents?|working with an agent)\b", re.I
    ),
    "price": re.compile(
        r"\b(?:asking price|list(?:ing)? price|worth|valuation|value of your home)\b", re.I
    ),
}

PRIOR_RELATIONSHIP = re.compile(
    r"\b(?:as (?:we )?discussed|following up|follow(?:ing)? up on|great to (?:meet|see|talk)|"
    r"nice to (?:meet|see|talk)|good to (?:see|hear from) you again|last time we|when we (?:spoke|met|talked)|"
    r"per our (?:conversation|call|chat)|as promised|circling back|touching base|reconnect|"
    r"since we last|you may remember me)\b",
    re.I,
)

# Fair-housing protected characteristics and steering language. Recipients are never described
# by these attributes, and the sender's own background never addresses or selects recipients.
PROTECTED = re.compile(
    r"\b(?:race|racial|ethnic\w*|religio\w*|church|temple|mosque|synagogue|gurdwara|national origin|"
    r"nationality|immigrant\w*|heritage|ancestry|caste|community like yours|people like you|"
    r"christian|muslim|hindu|jewish|sikh|buddhist|indian|asian|hispanic|latino|latina|latinx|"
    r"african american|your (?:family|kids|children|spouse|husband|wife|partner)|familial|"
    r"pregnan\w*|disab\w*|wheelchair|handicap\w*|your age|elderly|retiree\w*|gender|sexual orientation|"
    r"married|single (?:mom|mother|dad|father)|safe neighborhood|good schools)\b",
    re.I,
)

INQUIRY = re.compile(
    r"\b(?:inquir\w*|enquir\w*|reached out|reaching out|your (?:request|message|form|note)|"
    r"you (?:asked|wrote|contacted|submitted)|contact form|sign-?in sheet|open house)\b",
    re.I,
)


def split_sentences(body: str) -> list[str]:
    out: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            out.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip())
    return out


def _figures(text: str) -> list[str]:
    found: list[str] = []
    for pattern in FIGURE_RES:
        found.extend(m.group(0).strip() for m in pattern.finditer(text))
    return found


def _topic(label: str) -> str | None:
    low = label.casefold()
    for key, words in (
        ("budget", ("budget",)),
        ("timeline", ("timeline", "timing")),
        ("agent", ("agent",)),
        ("price", ("price",)),
    ):
        if any(w in low for w in words):
            return key
    return None


def email_code_checks(
    out: WriterOutput, wi: WriterInput, *, language: str, allowed_contacts: set[str]
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []

    def fail(rule: str, sentence: str | None, detail: str = "") -> None:
        failures.append({"rule": rule, "sentence": sentence, "detail": detail})

    sentences = [out.subject, *split_sentences(out.body)]
    words = len(re.findall(r"\b[\w'-]+\b", out.body))
    if words > wi.campaign.max_words:
        fail("max_words", None, f"{words} words > {wi.campaign.max_words}")
    if out.language != language:
        fail("language", None, f"{out.language} != {language}")

    allowed = {a.casefold() for a in allowed_contacts}
    for s in sentences:
        for pattern, rule in (
            (URL_RE, "link_not_configured"),
            (EMAIL_RE, "email_not_configured"),
            (PHONE_RE, "phone_not_configured"),
        ):
            for m in pattern.finditer(s):
                if m.group(0).casefold().rstrip(".,") not in allowed:
                    fail(rule, s, m.group(0))

    findings = {f.id: f.text for f in wi.findings}
    facts = {f.id: f.text for f in wi.sender.facts}
    refs_by_sentence: dict[str, list[str]] = {}
    for claim in out.claims:
        unknown = [r for r in claim.refs if r not in findings and r not in facts]
        if unknown:
            fail("unknown_reference", claim.sentence, ", ".join(unknown))
        if claim.sentence not in out.body and claim.sentence != out.subject:
            fail("claim_not_in_email", claim.sentence)
        refs_by_sentence[claim.sentence.strip()] = claim.refs

    always_allowed = " ".join([*facts.values(), wi.campaign.cta]).casefold()
    for s in sentences:
        cited = " ".join(findings.get(r, "") for r in refs_by_sentence.get(s, [])).casefold()
        for figure in _figures(s):
            if figure.casefold() not in cited and figure.casefold() not in always_allowed:
                fail("unsupported_figure_or_date", s, figure)

    for label in wi.unknowns:
        topic = _topic(label)
        if topic is None:
            continue
        for s in sentences:
            if UNKNOWN_TOPICS[topic].search(s):
                fail("mentions_unknown", s, label)

    for s in sentences:
        if prior := PRIOR_RELATIONSHIP.search(s):
            fail("implied_prior_relationship", s, prior.group(0))
        if protected := PROTECTED.search(s):
            fail("protected_attribute", s, protected.group(0))
        if not wi.outreach_context.may_mention_inquiry and (inquiry := INQUIRY.search(s)):
            fail("inquiry_not_permitted", s, inquiry.group(0))
    return failures
